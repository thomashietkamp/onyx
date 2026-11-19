"""Native passthrough of Anthropic Messages to a provider of type ``anthropic``.

Keeps what translation loses: server tools, signed thinking and the exact
token counts. All callers share one vendor account, so the request may not
refer to stored vendor state or make the vendor connect to a URL that the
caller chose.
"""

from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx
from fastapi import Response

from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.llm.constants import LlmProviderNames
from onyx.llm.models import Usage
from onyx.llm.multi_llm import LitellmLLM
from onyx.server.gateway import configs
from onyx.server.gateway.access import ResolvedGatewayModel
from onyx.server.gateway.anthropic_compat import anthropic_error_type
from onyx.server.gateway.models import AnthropicErrorEvent
from onyx.server.gateway.passthrough import (
    PassthroughObserver,
    SseFrame,
    UpstreamRequest,
    call_upstream_json,
    int_field,
    open_upstream_stream,
    relay_upstream_stream,
    unreachable_upstream,
)
from onyx.server.gateway.stream_bridge import sse_event, streaming_response
from onyx.server.gateway.translation import gateway_llm_error
from onyx.tracing.flows import LLMFlow
from onyx.utils.logger import setup_logger

logger = setup_logger()

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# Beta flags that change only how the model answers. A flag that reaches
# stored vendor state (files, skills, containers) or that connects to a
# caller URL (MCP, web fetch) is dropped.
_ALLOWED_BETA_PREFIXES = (
    "claude-code-",
    "context-1m-",
    "context-management-",
    "effort-",
    "extended-cache-ttl-",
    "fine-grained-tool-streaming-",
    "interleaved-thinking-",
    "output-128k-",
    "prompt-caching-",
    "structured-outputs-",
    "token-counting-",
    "token-efficient-tools-",
    "computer-use-",
)

# Top-level fields that refer to stored vendor state or a caller URL.
_REJECTED_FIELDS = ("mcp_servers", "container")
# Tool types that make the vendor fetch a URL from the request.
_REJECTED_TOOL_TYPE_PREFIXES = ("mcp", "web_fetch")
# Content block types and source types that refer to stored vendor files.
_REJECTED_BLOCK_TYPES = frozenset({"container_upload", "mcp_tool_use"})
_REJECTED_SOURCE_TYPES = frozenset({"file"})

# The fields the count_tokens endpoint takes.
_COUNT_TOKENS_FIELDS = (
    "messages",
    "system",
    "tools",
    "tool_choice",
    "thinking",
    "output_config",
)


def is_anthropic_passthrough_eligible(resolved: ResolvedGatewayModel) -> bool:
    return (
        configs.ANTHROPIC_GATEWAY_PASSTHROUGH_ENABLED
        and resolved.provider.provider == LlmProviderNames.ANTHROPIC
        and bool(resolved.provider.api_key)
    )


def _base_url(api_base: str | None) -> str:
    base = (api_base or _DEFAULT_BASE_URL).rstrip("/")
    return base.removesuffix("/v1")


def build_upstream_headers(api_key: str, inbound: Mapping[str, str]) -> dict[str, str]:
    """Fresh headers. Only the version and allowlisted beta flags come from
    the caller."""
    headers = {
        "x-api-key": api_key,
        "content-type": "application/json",
        "anthropic-version": inbound.get("anthropic-version")
        or _DEFAULT_ANTHROPIC_VERSION,
    }
    betas = [
        beta.strip()
        for beta in (inbound.get("anthropic-beta") or "").split(",")
        if beta.strip().startswith(_ALLOWED_BETA_PREFIXES)
    ]
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    return headers


def _refuse(message: str) -> OnyxError:
    return OnyxError(OnyxErrorCode.INVALID_INPUT, message)


def _check_content(messages: Any) -> None:
    if not isinstance(messages, list):
        return
    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") in _REJECTED_BLOCK_TYPES:
                raise _refuse(
                    f"Content block type {block.get('type')!r} is not supported "
                    "by the gateway."
                )
            source = block.get("source")
            if (
                isinstance(source, Mapping)
                and source.get("type") in _REJECTED_SOURCE_TYPES
            ):
                raise _refuse("File references are not supported by the gateway.")


def _check_tools(tools: Any) -> None:
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        tool_type = str(tool.get("type") or "")
        if tool_type.startswith(_REJECTED_TOOL_TYPE_PREFIXES) or any(
            key in tool for key in ("url", "server_url", "mcp_server_name")
        ):
            raise _refuse(f"Tool type {tool_type!r} is not supported by the gateway.")


def build_upstream_body(
    body: Mapping[str, Any], model_name: str, caller_hash: str
) -> dict[str, Any]:
    for field in _REJECTED_FIELDS:
        if body.get(field):
            raise _refuse(f"The {field} field is not supported by the gateway.")
    _check_content(body.get("messages"))
    _check_tools(body.get("tools"))
    upstream = dict(body)
    upstream["model"] = model_name
    # The caller identity field holds the hashed Onyx user, never the caller's.
    upstream["metadata"] = {"user_id": caller_hash}
    return upstream


def build_count_tokens_body(body: Mapping[str, Any], model_name: str) -> dict[str, Any]:
    _check_content(body.get("messages"))
    _check_tools(body.get("tools"))
    upstream = {key: body[key] for key in _COUNT_TOKENS_FIELDS if key in body}
    upstream["model"] = model_name
    return upstream


def anthropic_usage(usage: Mapping[str, Any] | None) -> Usage:
    """Anthropic input_tokens exclude cache reads and writes; Onyx usage uses
    the OpenAI accounting, where prompt tokens include both."""
    input_tokens = int_field(usage, "input_tokens")
    cache_read = int_field(usage, "cache_read_input_tokens")
    cache_creation = int_field(usage, "cache_creation_input_tokens")
    output_tokens = int_field(usage, "output_tokens")
    prompt_tokens = input_tokens + cache_read + cache_creation
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens,
        total_tokens=prompt_tokens + output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


def _observe_message(payload: dict[str, Any], observer: PassthroughObserver) -> None:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        observer.usage = anthropic_usage(usage)
    for block in payload.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            observer.text_parts.append(block["text"])


def _stream_observer() -> Callable[[SseFrame, PassthroughObserver], None]:
    # message_start carries the input counts. message_delta carries the
    # running output count and can update the input counts.
    usage_fields: dict[str, Any] = {}

    def merge_usage(usage: Any) -> None:
        if isinstance(usage, dict):
            usage_fields.update({k: v for k, v in usage.items() if v is not None})

    def observe(frame: SseFrame, observer: PassthroughObserver) -> None:
        data = frame.data
        if data is None:
            return
        event_type = data.get("type")
        if event_type == "message_start":
            message = data.get("message")
            if isinstance(message, dict):
                merge_usage(message.get("usage"))
        elif event_type == "message_delta":
            merge_usage(data.get("usage"))
        elif event_type == "content_block_delta":
            delta = data.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                observer.text_parts.append(delta["text"])
            return
        else:
            return
        observer.usage = anthropic_usage(usage_fields)

    return observe


def _upstream(
    resolved: ResolvedGatewayModel,
    path: str,
    body: dict[str, Any],
    inbound_headers: Mapping[str, str],
) -> UpstreamRequest:
    api_key = resolved.provider.api_key
    if not api_key:
        raise RuntimeError("Bug: passthrough eligibility requires an API key")
    return UpstreamRequest(
        url=f"{_base_url(resolved.provider.api_base)}{path}",
        headers=build_upstream_headers(api_key, inbound_headers),
        body=body,
        connect_timeout_s=configs.ANTHROPIC_PASSTHROUGH_CONNECT_TIMEOUT_SECONDS,
        read_timeout_s=configs.ANTHROPIC_PASSTHROUGH_READ_TIMEOUT_SECONDS,
    )


def anthropic_passthrough_messages(
    *,
    llm: LitellmLLM,
    flow: LLMFlow,
    resolved: ResolvedGatewayModel,
    body: Mapping[str, Any],
    inbound_headers: Mapping[str, str],
    caller_hash: str,
) -> Response:
    upstream_body = build_upstream_body(body, resolved.model_name, caller_hash)
    upstream = _upstream(resolved, "/v1/messages", upstream_body, inbound_headers)
    input_messages = upstream_body.get("messages")
    if not upstream_body.get("stream"):
        return call_upstream_json(
            llm=llm,
            flow=flow,
            wire_id=resolved.wire_id,
            upstream=upstream,
            input_messages=input_messages,
            observe=_observe_message,
        )
    opened = open_upstream_stream(upstream)
    if isinstance(opened, Response):
        return opened
    client, response = opened

    def error_frames(error: Exception) -> list[str]:
        onyx_error = gateway_llm_error(error, llm)
        payload = AnthropicErrorEvent.create(
            message=onyx_error.detail,
            error_type=anthropic_error_type(onyx_error.error_code),
        ).to_wire()
        return [sse_event("error", payload)]

    frames: Iterator[str] = relay_upstream_stream(
        llm=llm,
        flow=flow,
        wire_id=resolved.wire_id,
        client=client,
        response=response,
        input_messages=input_messages,
        observe=_stream_observer(),
        error_frames=error_frames,
        name="gateway-anthropic-passthrough-stream",
    )
    return streaming_response(frames)


def anthropic_passthrough_count_tokens(
    resolved: ResolvedGatewayModel,
    body: Mapping[str, Any],
    inbound_headers: Mapping[str, str],
) -> int | None:
    """The vendor's exact count, or None when the vendor cannot answer.

    Counting generates nothing, so it opens no span and is not metered.
    """
    upstream = _upstream(
        resolved,
        "/v1/messages/count_tokens",
        build_count_tokens_body(body, resolved.model_name),
        inbound_headers,
    )
    try:
        with upstream.client() as client:
            response = client.post(
                upstream.url, headers=upstream.headers, json=upstream.body
            )
    except httpx.HTTPError as e:
        unreachable_upstream(e)
        return None
    if response.status_code != httpx.codes.OK:
        logger.warning(
            "Gateway count_tokens passthrough got status %d", response.status_code
        )
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    count = payload.get("input_tokens") if isinstance(payload, dict) else None
    return count if isinstance(count, int) else None
