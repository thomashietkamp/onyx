"""The translated path shared by every dialect: one raw LiteLLM call inside a
gateway trace and a generation span."""

import time
from collections.abc import Callable, Generator
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue

from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.llm.exceptions import (
    LLMRateLimitError,
    LLMTimeoutError,
    litellm_exception_to_safe_error,
)
from onyx.llm.interfaces import LLMUserIdentity
from onyx.llm.model_request import ChatCompletionMessage
from onyx.llm.model_response import ModelResponse, ModelResponseStream
from onyx.llm.models import ReasoningEffort, ToolChoice, Usage
from onyx.llm.multi_llm import LitellmLLM
from onyx.server.gateway.configs import GATEWAY_LLM_TOTAL_TIMEOUT_SECONDS
from onyx.server.gateway.stream_bridge import StreamCancelled, ToolCallAccumulator
from onyx.server.gateway.tracing import llm_gateway_trace
from onyx.tracing.flows import LLMFlow
from onyx.tracing.framework.span_data import GenerationSpanData
from onyx.tracing.framework.spans import Span
from onyx.tracing.llm_utils import (
    llm_generation_span,
    record_llm_response,
    record_llm_span_output,
)

# Provider error classes that describe the caller's request, not the provider.
_CALLER_ERROR_CODES = frozenset(
    {
        "BAD_REQUEST",
        "CONTEXT_TOO_LONG",
        "CONTENT_POLICY",
        "UNPROCESSABLE_ENTITY",
        "REQUEST_TOO_LARGE",
    }
)

# Effort names of the OpenAI and Anthropic dialects, mapped to Onyx efforts.
_EFFORT_BY_NAME: dict[str, ReasoningEffort] = {
    "none": ReasoningEffort.OFF,
    "off": ReasoningEffort.OFF,
    "minimal": ReasoningEffort.LOW,
    "low": ReasoningEffort.LOW,
    "medium": ReasoningEffort.MEDIUM,
    "high": ReasoningEffort.HIGH,
    "xhigh": ReasoningEffort.XHIGH,
    "max": ReasoningEffort.XHIGH,
}


def reasoning_effort_from_name(name: str | None) -> ReasoningEffort:
    """An unknown or missing name leaves the choice to the model defaults."""
    if not name:
        return ReasoningEffort.AUTO
    return _EFFORT_BY_NAME.get(name.lower(), ReasoningEffort.AUTO)


class TranslatedCall(BaseModel):
    """One provider request in the shape that ``LitellmLLM`` takes."""

    model_config = ConfigDict(frozen=True)

    prompt: list[ChatCompletionMessage]
    tools: list[dict[str, JsonValue]] | None = None
    tool_choice: ToolChoice | None = None
    max_tokens: int | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO
    structured_response_format: dict[str, JsonValue] | None = None
    user_identity: LLMUserIdentity | None = None

    def input_messages(self) -> list[dict[str, Any]]:
        return [message.model_dump(exclude_none=True) for message in self.prompt]


def gateway_llm_error(error: Exception, llm: LitellmLLM) -> OnyxError:
    """A provider failure as an OnyxError with a redacted message."""
    if isinstance(error, OnyxError):
        return error
    if isinstance(error, LLMTimeoutError):
        return OnyxError(OnyxErrorCode.GATEWAY_TIMEOUT, "The model provider timed out.")
    if isinstance(error, LLMRateLimitError):
        return OnyxError(
            OnyxErrorCode.RATE_LIMITED, "The model provider rate limit was reached."
        )
    info = litellm_exception_to_safe_error(error, llm)
    if info.error_code == "RATE_LIMIT":
        return OnyxError(OnyxErrorCode.RATE_LIMITED, info.message)
    if info.error_code in _CALLER_ERROR_CODES:
        return OnyxError(OnyxErrorCode.INVALID_INPUT, info.message)
    return OnyxError(OnyxErrorCode.LLM_PROVIDER_ERROR, info.message)


def _set_span_error(span: Span[GenerationSpanData], error: OnyxError) -> None:
    span.set_error({"message": error.detail, "data": {"code": error.error_code.code}})


def invoke_translated(
    llm: LitellmLLM, flow: LLMFlow, wire_id: str, call: TranslatedCall
) -> ModelResponse:
    """One complete response. Raises OnyxError on a provider failure."""
    with llm_gateway_trace(flow, wire_id):
        with llm_generation_span(
            llm=llm,
            flow=flow,
            input_messages=call.input_messages(),
            tools=call.tools,
        ) as span:
            try:
                response = llm.invoke_raw(
                    prompt=call.prompt,
                    tools=call.tools,
                    tool_choice=call.tool_choice,
                    structured_response_format=call.structured_response_format,
                    max_tokens=call.max_tokens,
                    reasoning_effort=call.reasoning_effort,
                    user_identity=call.user_identity,
                    total_timeout_s=GATEWAY_LLM_TOTAL_TIMEOUT_SECONDS,
                )
            except Exception as e:
                onyx_error = gateway_llm_error(e, llm)
                _set_span_error(span, onyx_error)
                raise onyx_error from e
            record_llm_response(span, response)
            return response


class StreamSummary(BaseModel):
    """What a finished translated stream produced, for the dialect's last frames."""

    finish_reason: str | None = None
    usage: Usage | None = None
    content: str = ""
    reasoning: str = ""


def stream_translated(
    llm: LitellmLLM,
    flow: LLMFlow,
    wire_id: str,
    call: TranslatedCall,
    on_chunk: Callable[[ModelResponseStream], None],
) -> StreamSummary:
    """Stream one response and pass each provider chunk to ``on_chunk``.

    Call only on the stream worker thread: the trace and span live here.
    The span records the output and usage also when the client goes away.
    """
    summary = StreamSummary()
    tool_calls = ToolCallAccumulator()
    deadline = time.monotonic() + GATEWAY_LLM_TOTAL_TIMEOUT_SECONDS
    with llm_gateway_trace(flow, wire_id):
        with llm_generation_span(
            llm=llm,
            flow=flow,
            input_messages=call.input_messages(),
            tools=call.tools,
        ) as span:
            stream = llm.stream_raw(
                prompt=call.prompt,
                tools=call.tools,
                tool_choice=call.tool_choice,
                structured_response_format=call.structured_response_format,
                max_tokens=call.max_tokens,
                reasoning_effort=call.reasoning_effort,
                user_identity=call.user_identity,
            )
            try:
                for chunk in stream:
                    delta = chunk.choice.delta
                    if delta.content:
                        summary.content += delta.content
                    if delta.reasoning_content:
                        summary.reasoning += delta.reasoning_content
                    for tool_delta in delta.tool_calls:
                        tool_calls.merge(tool_delta)
                    if chunk.choice.finish_reason:
                        summary.finish_reason = chunk.choice.finish_reason
                    if chunk.usage is not None:
                        summary.usage = chunk.usage
                    on_chunk(chunk)
                    if time.monotonic() > deadline:
                        raise LLMTimeoutError(
                            "The gateway stream exceeded its total time limit."
                        )
            except StreamCancelled:
                raise
            except Exception as e:
                onyx_error = gateway_llm_error(e, llm)
                _set_span_error(span, onyx_error)
                raise onyx_error from e
            finally:
                if isinstance(stream, Generator):
                    stream.close()
                record_llm_span_output(
                    span,
                    output=summary.content or None,
                    usage=summary.usage,
                    reasoning=summary.reasoning or None,
                    tool_calls=tool_calls.tool_calls() or None,
                )
    return summary
