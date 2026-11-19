"""Who may call the gateway, under which flow, and which model a wire id names."""

import hashlib

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.enums import Permission
from onyx.db.llm import fetch_accessible_llm_provider_by_id
from onyx.db.models import User
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.features.build.craft_gateway import gateway_request_flow
from onyx.server.manage.llm.models import LLMProviderView, ModelConfigurationView
from onyx.server.query_and_chat.token_limit import (
    check_model_spend_limits,
    check_token_rate_limits,
)
from onyx.server.settings.store import load_settings
from onyx.server.usage_limits import check_llm_cost_limit_for_provider
from onyx.tracing.flows import LLMFlow
from shared_configs.contextvars import get_current_tenant_id

# The permission check. It admits almost every user, so each endpoint also
# runs the flow check in authorize_gateway_request.
gateway_user = require_permission(Permission.USE_LLM_GATEWAY)


class ResolvedGatewayModel(BaseModel):
    """A wire model id resolved to a provider row the caller may use."""

    model_config = ConfigDict(frozen=True)

    wire_id: str
    provider: LLMProviderView
    model: ModelConfigurationView

    @property
    def model_name(self) -> str:
        return self.model.name


def authorize_gateway_request(request: Request, user: User) -> LLMFlow:
    """The flow check: a token scope must grant the gateway.

    Cookie sessions, API keys and unscoped PATs have no token scopes and get
    no flow. The workspace switch applies only to the LLM_GATEWAY flow. Craft
    tokens follow the Craft setting instead.
    """
    flow = gateway_request_flow(request, user)
    if flow is None:
        raise OnyxError(
            OnyxErrorCode.INSUFFICIENT_PERMISSIONS,
            "The LLM gateway requires a personal access token with the "
            "use:llm_gateway scope.",
        )
    # Fail closed: an unreadable settings store must not open the gateway.
    if (
        flow is LLMFlow.LLM_GATEWAY
        and not load_settings(raise_on_error=True).llm_gateway_enabled
    ):
        raise OnyxError(
            OnyxErrorCode.FEATURE_DISABLED,
            "The LLM gateway is disabled for this workspace.",
        )
    return flow


def _model_not_found(wire_id: str) -> OnyxError:
    return OnyxError(
        OnyxErrorCode.NOT_FOUND,
        f"Model '{wire_id}' is not available. List the available models with "
        "GET /v1/models.",
    )


def resolve_gateway_model(
    db_session: Session, user: User, wire_id: str
) -> ResolvedGatewayModel:
    """Resolve ``<provider_id>/<model_name>`` to a visible chat model.

    An unknown or inaccessible id is a 404. There is no fallback model: the
    caller chose the model, and a substitute bills a different budget.
    """
    provider_id_text, separator, model_name = wire_id.partition("/")
    if not separator or not model_name or not provider_id_text.isdigit():
        raise _model_not_found(wire_id)
    provider = fetch_accessible_llm_provider_by_id(
        db_session, user, int(provider_id_text)
    )
    if provider is None:
        raise _model_not_found(wire_id)
    model = next(
        (
            model
            for model in provider.model_configurations
            if model.name == model_name and model.is_visible and model.supports_chat
        ),
        None,
    )
    if model is None:
        raise _model_not_found(wire_id)
    return ResolvedGatewayModel(wire_id=wire_id, provider=provider, model=model)


def resolve_metered_gateway_model(
    db_session: Session, user: User, wire_id: str
) -> ResolvedGatewayModel:
    """Resolution for every endpoint that generates.

    Checks the all-models spending limits first, then the cloud cost cap on
    Onyx-managed keys and the spending limits on the resolved model.
    """
    check_token_rate_limits(user)
    resolved = resolve_gateway_model(db_session, user, wire_id)
    check_llm_cost_limit_for_provider(
        db_session=db_session,
        tenant_id=get_current_tenant_id(),
        llm_provider_api_key=resolved.provider.api_key,
    )
    check_model_spend_limits(user, resolved.model.id)
    return resolved


def caller_identity_hash(user: User) -> str:
    """A stable, opaque id for the caller, sent to vendors in place of any
    caller-supplied identity field. All callers share one vendor account."""
    digest = hashlib.sha256(
        f"{get_current_tenant_id()}:{user.id}".encode("utf-8")
    ).hexdigest()
    return f"onyx-{digest[:32]}"
