"""The LLM gateway: OpenAI- and Anthropic-compatible endpoints over the
providers that Onyx has configured.

Every generating endpoint runs the same steps in this order: the permission
check (router dependency), the flow check, the all-models spending limits,
metered model resolution, and then the passthrough or the translated path.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.db.engine.sql_engine import get_session
from onyx.db.llm import fetch_all_accessible_llm_providers
from onyx.db.models import User
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.llm.factory import llm_from_provider
from onyx.llm.multi_llm import LitellmLLM
from onyx.server.gateway.access import (
    ResolvedGatewayModel,
    authorize_gateway_request,
    caller_identity_hash,
    gateway_user,
    resolve_gateway_model,
    resolve_metered_gateway_model,
)
from onyx.server.gateway.anthropic_compat import (
    build_messages_call,
    estimate_input_tokens,
    messages_completion,
    messages_stream,
)
from onyx.server.gateway.anthropic_passthrough import (
    anthropic_passthrough_count_tokens,
    anthropic_passthrough_messages,
    is_anthropic_passthrough_eligible,
)
from onyx.server.gateway.configs import GATEWAY_PATH_PREFIX
from onyx.server.gateway.craft_session import craft_session_binder
from onyx.server.gateway.model_catalog import build_gateway_model_catalog
from onyx.server.gateway.models import (
    AnthropicCountTokensRequest,
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    ModelListResponse,
    ResponsesRequest,
)
from onyx.server.gateway.openai_compat import (
    build_chat_call,
    chat_completion,
    chat_completion_stream,
)
from onyx.server.gateway.openai_passthrough import (
    is_openai_passthrough_eligible,
    openai_passthrough_responses,
)
from onyx.server.gateway.responses_compat import (
    build_responses_call,
    responses_completion,
    responses_stream,
)
from onyx.server.gateway.stream_bridge import streaming_response

router = APIRouter(
    prefix=GATEWAY_PATH_PREFIX,
    dependencies=[Depends(craft_session_binder(gateway_user))],
)


def _gateway_llm(
    resolved: ResolvedGatewayModel, temperature: float | None
) -> LitellmLLM:
    try:
        return llm_from_provider(
            model_name=resolved.model_name,
            llm_provider=resolved.provider,
            temperature=temperature,
        )
    except ValueError as e:
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, str(e)) from e


def _request_body(request_model: BaseModel) -> dict[str, Any]:
    """The caller's fields as sent, unknown fields included."""
    return request_model.model_dump(mode="json", exclude_unset=True)


@router.get("/v1/models")
def gateway_list_models(
    http_request: Request,
    user: User = Depends(gateway_user),
    db_session: Session = Depends(get_session),
) -> dict[str, Any]:
    authorize_gateway_request(http_request, user)
    providers = fetch_all_accessible_llm_providers(db_session, user)
    return ModelListResponse.from_catalog(
        build_gateway_model_catalog(providers)
    ).to_wire()


@router.post("/v1/chat/completions")
def gateway_chat_completions(
    chat_request: ChatCompletionRequest,
    http_request: Request,
    user: User = Depends(gateway_user),
    db_session: Session = Depends(get_session),
) -> Response:
    flow = authorize_gateway_request(http_request, user)
    resolved = resolve_metered_gateway_model(db_session, user, chat_request.model)
    llm = _gateway_llm(resolved, chat_request.temperature)
    call = build_chat_call(chat_request, caller_identity_hash(user))
    if chat_request.stream:
        return streaming_response(
            chat_completion_stream(llm, flow, resolved.wire_id, call)
        )
    return JSONResponse(content=chat_completion(llm, flow, resolved.wire_id, call))


@router.post("/v1/responses")
def gateway_responses(
    responses_request: ResponsesRequest,
    http_request: Request,
    user: User = Depends(gateway_user),
    db_session: Session = Depends(get_session),
) -> Response:
    flow = authorize_gateway_request(http_request, user)
    if responses_request.previous_response_id:
        raise OnyxError(
            OnyxErrorCode.NOT_IMPLEMENTED,
            "previous_response_id is not supported: the gateway is stateless. "
            "Send the full conversation in input.",
        )
    resolved = resolve_metered_gateway_model(db_session, user, responses_request.model)
    llm = _gateway_llm(resolved, responses_request.temperature)
    if is_openai_passthrough_eligible(resolved):
        return openai_passthrough_responses(
            llm=llm,
            flow=flow,
            resolved=resolved,
            body=_request_body(responses_request),
            caller_hash=caller_identity_hash(user),
        )
    call = build_responses_call(responses_request, caller_identity_hash(user))
    if responses_request.stream:
        return streaming_response(responses_stream(llm, flow, resolved.wire_id, call))
    return JSONResponse(content=responses_completion(llm, flow, resolved.wire_id, call))


@router.post("/v1/messages")
def gateway_anthropic_messages(
    messages_request: AnthropicMessagesRequest,
    http_request: Request,
    user: User = Depends(gateway_user),
    db_session: Session = Depends(get_session),
) -> Response:
    flow = authorize_gateway_request(http_request, user)
    resolved = resolve_metered_gateway_model(db_session, user, messages_request.model)
    llm = _gateway_llm(resolved, messages_request.temperature)
    if is_anthropic_passthrough_eligible(resolved):
        return anthropic_passthrough_messages(
            llm=llm,
            flow=flow,
            resolved=resolved,
            body=_request_body(messages_request),
            inbound_headers=http_request.headers,
            caller_hash=caller_identity_hash(user),
        )
    call = build_messages_call(messages_request, caller_identity_hash(user))
    if messages_request.stream:
        return streaming_response(messages_stream(llm, flow, resolved.wire_id, call))
    return JSONResponse(content=messages_completion(llm, flow, resolved.wire_id, call))


@router.post("/v1/messages/count_tokens")
def gateway_anthropic_count_tokens(
    count_request: AnthropicCountTokensRequest,
    http_request: Request,
    user: User = Depends(gateway_user),
    db_session: Session = Depends(get_session),
) -> dict[str, int]:
    """Counting generates nothing, so it skips the spending limits. When the
    vendor cannot count, the answer is a local estimate, not an error."""
    authorize_gateway_request(http_request, user)
    resolved = resolve_gateway_model(db_session, user, count_request.model)
    if is_anthropic_passthrough_eligible(resolved):
        count = anthropic_passthrough_count_tokens(
            resolved, _request_body(count_request), http_request.headers
        )
        if count is not None:
            return {"input_tokens": count}
    llm = _gateway_llm(resolved, None)
    return {"input_tokens": estimate_input_tokens(llm, count_request)}
