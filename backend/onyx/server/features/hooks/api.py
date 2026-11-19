"""Admin API for hook extensions. Self-hosted only (``require_hook_enabled``).

Saving and activating a hook call its endpoint first: a network error, a
timeout, or a 401 or 403 refuses the change, so an endpoint that cannot be
reached never becomes active. Any other HTTP status passes.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.constants import UNSET, UnsetType
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.hook import (
    create_hook__no_commit,
    delete_hook__no_commit,
    get_hook_by_id,
    get_hook_execution_logs,
    get_hooks,
    update_hook__no_commit,
)
from onyx.db.models import Hook, User
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.hooks.api_dependencies import require_hook_enabled
from onyx.hooks.models import (
    HookCreateRequest,
    HookExecutionRecord,
    HookPointMetaResponse,
    HookResponse,
    HookUpdateRequest,
    HookValidateResponse,
    HookValidateStatus,
)
from onyx.hooks.registry import get_all_specs, get_hook_point_spec
from onyx.server.security.models import outbound_ssrf_params
from onyx.server.security.store import get_security_settings
from onyx.utils.encryption import mask_string
from onyx.utils.external_endpoint import ExternalEndpointConfig, post_json_to_endpoint
from onyx.utils.url import SSRFException, validate_outbound_http_url

router = APIRouter(
    prefix="/admin/hooks",
    dependencies=[Depends(require_hook_enabled)],
)

_admin = require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)

_MAX_EXECUTION_LOGS = 100


def _check_ssrf_safety(endpoint_url: str) -> str:
    """HTTPS always, and the admin's SSRF protection level. A hostname that is
    public now can resolve to a private address later; delivery does not check
    again."""
    params = outbound_ssrf_params(get_security_settings().ssrf_protection_level)
    try:
        return validate_outbound_http_url(
            endpoint_url,
            https_only=True,
            allow_private_network=params.allow_private_network,
            block_loopback_and_link_local=params.block_loopback_and_link_local,
            block_link_local_only=params.block_link_local_only,
        )
    except (SSRFException, ValueError) as e:
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, f"Endpoint URL: {e}") from e


def _validate_endpoint(
    endpoint_url: str, api_key: str | None, timeout_seconds: float
) -> HookValidateResponse:
    outcome, _ = post_json_to_endpoint(
        config=ExternalEndpointConfig(
            endpoint_url=endpoint_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        ),
        payload={},
    )
    if outcome.status_code in (401, 403):
        status = HookValidateStatus.auth_failed
    elif outcome.status_code is not None:
        return HookValidateResponse(status=HookValidateStatus.passed)
    elif outcome.reachability_signal is None and "timed out" in (
        outcome.error_message or ""
    ):
        status = HookValidateStatus.timeout
    else:
        status = HookValidateStatus.cannot_connect
    return HookValidateResponse(status=status, error_message=outcome.error_message)


def _refuse_unless_passed(result: HookValidateResponse) -> None:
    if result.status != HookValidateStatus.passed:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            f"The endpoint check failed ({result.status.value}): "
            f"{result.error_message or 'no detail'}",
        )


def _api_key(hook: Hook) -> str | None:
    return hook.api_key.get_value(apply_mask=False) if hook.api_key else None


def _response(hook: Hook) -> HookResponse:
    api_key = _api_key(hook)
    return HookResponse(
        id=hook.id,
        name=hook.name,
        hook_point=hook.hook_point,
        endpoint_url=hook.endpoint_url,
        api_key_masked=mask_string(api_key) if api_key else None,
        fail_strategy=hook.fail_strategy,
        timeout_seconds=hook.timeout_seconds,
        is_active=hook.is_active,
        is_reachable=hook.is_reachable,
        creator_email=hook.creator.email if hook.creator else None,
        created_at=hook.created_at,
        updated_at=hook.updated_at,
    )


def _hook_or_404(db_session: Session, hook_id: int) -> Hook:
    hook = get_hook_by_id(db_session=db_session, hook_id=hook_id, include_creator=True)
    if hook is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, f"Hook with id {hook_id} not found.")
    return hook


@router.get("/specs")
def list_hook_specs(_: User = Depends(_admin)) -> list[HookPointMetaResponse]:
    return [
        HookPointMetaResponse(
            hook_point=spec.hook_point,
            display_name=spec.display_name,
            description=spec.description,
            docs_url=spec.docs_url,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            default_timeout_seconds=spec.default_timeout_seconds,
            default_fail_strategy=spec.default_fail_strategy,
            fail_hard_description=spec.fail_hard_description,
        )
        for spec in get_all_specs()
    ]


@router.get("")
def list_hooks(
    _: User = Depends(_admin), db_session: Session = Depends(get_session)
) -> list[HookResponse]:
    return [
        _response(hook)
        for hook in get_hooks(db_session=db_session, include_creator=True)
    ]


@router.post("")
def create_hook(
    request: HookCreateRequest,
    user: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> HookResponse:
    spec = get_hook_point_spec(request.hook_point)
    endpoint_url = _check_ssrf_safety(request.endpoint_url)
    api_key = request.api_key.get_secret_value() if request.api_key else None
    timeout_seconds = request.timeout_seconds or spec.default_timeout_seconds
    _refuse_unless_passed(_validate_endpoint(endpoint_url, api_key, timeout_seconds))
    hook = create_hook__no_commit(
        db_session=db_session,
        name=request.name.strip(),
        hook_point=request.hook_point,
        endpoint_url=endpoint_url,
        api_key=api_key,
        fail_strategy=request.fail_strategy or spec.default_fail_strategy,
        timeout_seconds=timeout_seconds,
        is_active=True,
        is_reachable=True,
        creator_id=user.id,
    )
    db_session.commit()
    return _response(_hook_or_404(db_session, hook.id))


@router.get("/{hook_id}")
def get_hook(
    hook_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> HookResponse:
    return _response(_hook_or_404(db_session, hook_id))


@router.patch("/{hook_id}")
def update_hook(
    hook_id: int,
    request: HookUpdateRequest,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> HookResponse:
    """A change to the URL, key or timeout calls the endpoint again. A failing
    edit is refused for an active hook; deactivate it first to edit it."""
    hook = _hook_or_404(db_session, hook_id)
    fields = request.model_fields_set
    endpoint_url: str | None | UnsetType = UNSET
    api_key: str | None | UnsetType = UNSET
    if "endpoint_url" in fields and request.endpoint_url is not None:
        endpoint_url = _check_ssrf_safety(request.endpoint_url)
    if "api_key" in fields:
        api_key = request.api_key.get_secret_value() if request.api_key else None

    is_reachable: bool | None = None
    if fields & {"endpoint_url", "api_key", "timeout_seconds"}:
        url = endpoint_url if isinstance(endpoint_url, str) else hook.endpoint_url or ""
        key = api_key if not isinstance(api_key, UnsetType) else _api_key(hook)
        result = _validate_endpoint(
            url, key, request.timeout_seconds or hook.timeout_seconds
        )
        if hook.is_active:
            _refuse_unless_passed(result)
        is_reachable = result.status == HookValidateStatus.passed

    update_hook__no_commit(
        db_session=db_session,
        hook_id=hook_id,
        name=request.name.strip() if request.name else None,
        endpoint_url=endpoint_url,
        api_key=api_key,
        fail_strategy=request.fail_strategy,
        timeout_seconds=request.timeout_seconds,
        is_reachable=is_reachable,
    )
    db_session.commit()
    return _response(_hook_or_404(db_session, hook_id))


@router.delete("/{hook_id}")
def delete_hook(
    hook_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> None:
    delete_hook__no_commit(db_session=db_session, hook_id=hook_id)
    db_session.commit()


@router.post("/{hook_id}/activate")
def activate_hook(
    hook_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> HookResponse:
    hook = _hook_or_404(db_session, hook_id)
    if not hook.endpoint_url:
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, "The hook has no endpoint URL")
    _refuse_unless_passed(
        _validate_endpoint(hook.endpoint_url, _api_key(hook), hook.timeout_seconds)
    )
    update_hook__no_commit(
        db_session=db_session, hook_id=hook_id, is_active=True, is_reachable=True
    )
    db_session.commit()
    return _response(_hook_or_404(db_session, hook_id))


@router.post("/{hook_id}/deactivate")
def deactivate_hook(
    hook_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> HookResponse:
    _hook_or_404(db_session, hook_id)
    update_hook__no_commit(db_session=db_session, hook_id=hook_id, is_active=False)
    db_session.commit()
    return _response(_hook_or_404(db_session, hook_id))


@router.post("/{hook_id}/validate")
def validate_hook(
    hook_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> HookValidateResponse:
    hook = _hook_or_404(db_session, hook_id)
    if not hook.endpoint_url:
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, "The hook has no endpoint URL")
    result = _validate_endpoint(hook.endpoint_url, _api_key(hook), hook.timeout_seconds)
    reachable = result.status == HookValidateStatus.passed
    if reachable != hook.is_reachable:
        update_hook__no_commit(
            db_session=db_session, hook_id=hook_id, is_reachable=reachable
        )
        db_session.commit()
    return result


@router.get("/{hook_id}/execution-logs")
def list_hook_execution_logs(
    hook_id: int,
    limit: int = Query(20, ge=1, le=_MAX_EXECUTION_LOGS),
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> list[HookExecutionRecord]:
    """Recent failures. A success writes no row."""
    _hook_or_404(db_session, hook_id)
    return [
        HookExecutionRecord(
            error_message=log.error_message,
            status_code=log.status_code,
            duration_ms=log.duration_ms,
            created_at=log.created_at,
        )
        for log in get_hook_execution_logs(
            db_session=db_session, hook_id=hook_id, limit=limit
        )
    ]
