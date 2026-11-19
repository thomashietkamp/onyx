"""Hook execution.

execute_hook is the public entry point. It dispatches to _execute_hook_impl
through fetch_versioned_implementation, so a build that ships its own
executor can replace this one without changes at the call sites.

The executor posts the payload to the hook's endpoint and validates the
response. A failure follows the hook's fail strategy: SOFT returns
HookSoftFailed so the caller keeps its default; HARD raises. Only failures are
logged, in their own session, so a busy hook point adds no write per call.
"""

from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.enums import HookFailStrategy, HookPoint
from onyx.db.hook import (
    create_hook_execution_log__no_commit,
    get_non_deleted_hook_by_hook_point,
    update_hook__no_commit,
)
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.utils.external_endpoint import ExternalEndpointConfig, post_json_to_endpoint
from onyx.utils.logger import setup_logger
from onyx.utils.variable_functionality import fetch_versioned_implementation
from shared_configs.configs import MULTI_TENANT

logger = setup_logger()


class HookSkipped:
    """No active hook configured for this hook point."""


class HookSoftFailed:
    """Hook was called but failed with SOFT fail strategy — continuing."""


T = TypeVar("T", bound=BaseModel)


class _HookTarget(BaseModel):
    """The fields of the hook the call needs, copied so the HTTP call holds no
    ORM object."""

    id: int
    name: str
    endpoint_url: str
    api_key: str | None
    timeout_seconds: float
    fail_strategy: HookFailStrategy
    is_reachable: bool | None


def _lookup_hook(db_session: Session, hook_point: HookPoint) -> _HookTarget | None:
    if MULTI_TENANT:
        return None
    hook = get_non_deleted_hook_by_hook_point(
        db_session=db_session, hook_point=hook_point
    )
    if hook is None or not hook.is_active or not hook.endpoint_url:
        return None
    return _HookTarget(
        id=hook.id,
        name=hook.name,
        endpoint_url=hook.endpoint_url,
        api_key=hook.api_key.get_value(apply_mask=False) if hook.api_key else None,
        timeout_seconds=hook.timeout_seconds,
        fail_strategy=hook.fail_strategy,
        is_reachable=hook.is_reachable,
    )


def _record_failure(
    target: _HookTarget,
    error_message: str | None,
    status_code: int | None,
    duration_ms: int,
) -> None:
    try:
        with get_session_with_current_tenant() as session:
            create_hook_execution_log__no_commit(
                db_session=session,
                hook_id=target.id,
                is_success=False,
                error_message=error_message,
                status_code=status_code,
                duration_ms=duration_ms,
            )
            session.commit()
    except Exception:
        logger.exception("Could not log the failure of hook %s", target.id)


def _record_reachability(target: _HookTarget, reachable: bool) -> None:
    """Own session: a hook deleted meanwhile makes this fail, and the failure
    log written before stays."""
    try:
        with get_session_with_current_tenant() as session:
            update_hook__no_commit(
                db_session=session, hook_id=target.id, is_reachable=reachable
            )
            session.commit()
    except Exception:
        logger.warning("Could not update the reachability of hook %s", target.id)


def _execute_hook_impl(
    *,
    db_session: Session,
    hook_point: HookPoint,
    payload: dict[str, Any],
    response_type: type[T],
) -> T | HookSkipped | HookSoftFailed:
    target = _lookup_hook(db_session, hook_point)
    if target is None:
        return HookSkipped()

    outcome, model = post_json_to_endpoint(
        config=ExternalEndpointConfig(
            endpoint_url=target.endpoint_url,
            api_key=target.api_key,
            timeout_seconds=target.timeout_seconds,
        ),
        payload=payload,
        response_type=response_type,
    )
    if not outcome.is_success:
        _record_failure(
            target, outcome.error_message, outcome.status_code, outcome.duration_ms
        )
    if (
        outcome.reachability_signal is not None
        and outcome.reachability_signal != target.is_reachable
    ):
        _record_reachability(target, outcome.reachability_signal)

    if outcome.is_success and model is not None:
        return model
    if target.fail_strategy == HookFailStrategy.SOFT:
        logger.warning(
            "Hook %s failed; continuing (SOFT): %s", target.id, outcome.error_message
        )
        return HookSoftFailed()
    raise OnyxError(
        OnyxErrorCode.HOOK_EXECUTION_FAILED,
        f"Hook '{target.name}' failed: {outcome.error_message or 'no valid response'}",
    )


def execute_hook(
    *,
    db_session: Session,
    hook_point: HookPoint,
    payload: dict[str, Any],
    response_type: type[T],
) -> T | HookSkipped | HookSoftFailed:
    """Execute the hook for the given hook point.

    Dispatches to the versioned implementation, so a build with its own
    executor needs no change at the call site.
    """
    impl = fetch_versioned_implementation("onyx.hooks.executor", "_execute_hook_impl")
    return impl(
        db_session=db_session,
        hook_point=hook_point,
        payload=payload,
        response_type=response_type,
    )
