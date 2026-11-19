"""Native passthrough of OpenAI Responses for true OpenAI models.

Keeps what translation loses: hosted tools and encrypted reasoning. All
callers share one vendor account, so the request is stateless (``store`` is
always false) and may not refer to stored vendor state or make the vendor
connect to a URL that the caller chose.
"""

from collections.abc import Callable, Iterator, Mapping
from typing import Any

from fastapi import Response

from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.llm.constants import LlmProviderNames
from onyx.llm.model_capabilities import is_true_openai_model
from onyx.llm.models import Usage
from onyx.llm.multi_llm import LitellmLLM
from onyx.server.gateway import configs
from onyx.server.gateway.access import ResolvedGatewayModel
from onyx.server.gateway.passthrough import (
    PassthroughObserver,
    SseFrame,
    UpstreamRequest,
    call_upstream_json,
    int_field,
    open_upstream_stream,
    relay_upstream_stream,
)
from onyx.server.gateway.stream_bridge import sse_event, streaming_response
from onyx.server.gateway.translation import gateway_llm_error
from onyx.tracing.flows import LLMFlow

_DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Top-level fields that refer to stored vendor state.
_REJECTED_FIELDS = ("conversation", "prompt", "background")
# Hosted tools that read stored vendor data or connect to a caller URL.
_REJECTED_TOOL_TYPES = frozenset({"mcp", "file_search"})
_URL_TOOL_KEYS = ("server_url", "connector_id")
# Input items that refer to stored vendor items.
_REJECTED_INPUT_ITEM_TYPES = frozenset({"item_reference"})
# Fields the vendor reads as the caller's identity.
_IDENTITY_FIELDS = ("user", "safety_identifier")


def is_openai_passthrough_eligible(resolved: ResolvedGatewayModel) -> bool:
    provider = resolved.provider
    return (
        configs.OPENAI_GATEWAY_PASSTHROUGH_ENABLED
        and provider.provider == LlmProviderNames.OPENAI
        and bool(provider.api_key)
        and is_true_openai_model(provider.provider, resolved.model_name)
    )


def _base_url(api_base: str | None) -> str:
    base = (api_base or _DEFAULT_BASE_URL).rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def build_upstream_headers(api_key: str) -> dict[str, str]:
    """Fresh headers. No caller header is sent: an OpenAI-Organization or
    OpenAI-Project header would select a billing scope of the caller's."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _refuse(message: str) -> OnyxError:
    return OnyxError(OnyxErrorCode.INVALID_INPUT, message)


def _check_tools(tools: Any) -> None:
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        tool_type = tool.get("type")
        if tool_type in _REJECTED_TOOL_TYPES or any(
            key in tool for key in _URL_TOOL_KEYS
        ):
            raise _refuse(f"Tool type {tool_type!r} is not supported by the gateway.")
        if tool_type == "code_interpreter" and isinstance(tool.get("container"), str):
            raise _refuse("A stored code interpreter container is not supported.")


def _has_file_reference(part: Any) -> bool:
    return isinstance(part, Mapping) and bool(part.get("file_id"))


def _check_input(request_input: Any) -> None:
    if not isinstance(request_input, list):
        return
    for item in request_input:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") in _REJECTED_INPUT_ITEM_TYPES:
            raise _refuse("Item references are not supported by the gateway.")
        content = item.get("content")
        parts = content if isinstance(content, list) else [item]
        if any(_has_file_reference(part) for part in parts):
            raise _refuse("File references are not supported by the gateway.")


def build_upstream_body(
    body: Mapping[str, Any], model_name: str, caller_hash: str
) -> dict[str, Any]:
    if body.get("previous_response_id"):
        raise OnyxError(
            OnyxErrorCode.NOT_IMPLEMENTED,
            "previous_response_id is not supported: the gateway is stateless. "
            "Send the full conversation in input.",
        )
    for field in _REJECTED_FIELDS:
        if body.get(field):
            raise _refuse(f"The {field} field is not supported by the gateway.")
    _check_tools(body.get("tools"))
    _check_input(body.get("input"))
    upstream = {
        key: value
        for key, value in body.items()
        if key not in _IDENTITY_FIELDS and key != "metadata"
    }
    upstream["model"] = model_name
    upstream["store"] = False
    upstream["safety_identifier"] = caller_hash
    cache_key = body.get("prompt_cache_key")
    if cache_key:
        # Keep prompt cache entries apart between callers.
        upstream["prompt_cache_key"] = f"{caller_hash}:{cache_key}"
    return upstream


def responses_usage(usage: Mapping[str, Any] | None) -> Usage:
    """Responses input_tokens already include the cached tokens."""
    details = usage.get("input_tokens_details") if usage else None
    input_tokens = int_field(usage, "input_tokens")
    output_tokens = int_field(usage, "output_tokens")
    return Usage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=int_field(usage, "total_tokens") or input_tokens + output_tokens,
        cache_read_input_tokens=int_field(
            details if isinstance(details, Mapping) else None, "cached_tokens"
        ),
        cache_creation_input_tokens=0,
    )


def _observe_response(payload: dict[str, Any], observer: PassthroughObserver) -> None:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        observer.usage = responses_usage(usage)
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                observer.text_parts.append(part["text"])


_TERMINAL_EVENTS = frozenset(
    {"response.completed", "response.incomplete", "response.failed"}
)


def _observe_frame(frame: SseFrame, observer: PassthroughObserver) -> None:
    data = frame.data
    if data is None:
        return
    event_type = data.get("type")
    if event_type == "response.output_text.delta" and isinstance(
        data.get("delta"), str
    ):
        observer.text_parts.append(data["delta"])
    elif event_type in _TERMINAL_EVENTS:
        response = data.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict):
            observer.usage = responses_usage(usage)


def _error_frames_for(
    llm: LitellmLLM,
) -> Callable[[Exception], list[str]]:
    def error_frames(error: Exception) -> list[str]:
        onyx_error = gateway_llm_error(error, llm)
        return [
            sse_event(
                "error",
                {
                    "type": "error",
                    "code": "server_error",
                    "message": onyx_error.detail,
                    "param": None,
                },
            )
        ]

    return error_frames


def openai_passthrough_responses(
    *,
    llm: LitellmLLM,
    flow: LLMFlow,
    resolved: ResolvedGatewayModel,
    body: Mapping[str, Any],
    caller_hash: str,
) -> Response:
    api_key = resolved.provider.api_key
    if not api_key:
        raise RuntimeError("Bug: passthrough eligibility requires an API key")
    upstream_body = build_upstream_body(body, resolved.model_name, caller_hash)
    upstream = UpstreamRequest(
        url=f"{_base_url(resolved.provider.api_base)}/responses",
        headers=build_upstream_headers(api_key),
        body=upstream_body,
        connect_timeout_s=configs.OPENAI_PASSTHROUGH_CONNECT_TIMEOUT_SECONDS,
        read_timeout_s=configs.OPENAI_PASSTHROUGH_READ_TIMEOUT_SECONDS,
    )
    input_messages = upstream_body.get("input")
    if not upstream_body.get("stream"):
        return call_upstream_json(
            llm=llm,
            flow=flow,
            wire_id=resolved.wire_id,
            upstream=upstream,
            input_messages=input_messages,
            observe=_observe_response,
        )
    opened = open_upstream_stream(upstream)
    if isinstance(opened, Response):
        return opened
    client, response = opened
    frames: Iterator[str] = relay_upstream_stream(
        llm=llm,
        flow=flow,
        wire_id=resolved.wire_id,
        client=client,
        response=response,
        input_messages=input_messages,
        observe=_observe_frame,
        error_frames=_error_frames_for(llm),
        name="gateway-openai-passthrough-stream",
    )
    return streaming_response(frames)
