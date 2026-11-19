"""Anthropic Messages on the translated path.

Used for every provider that is not of type ``anthropic``, and for Anthropic
providers when the passthrough is off. Server tools and signed thinking need
the passthrough: this path refuses server tools and sends a thinking block
only when the provider returned a signature for it.
"""

import json
from collections.abc import Iterator, Mapping
from typing import Any, Literal
from uuid import uuid4

from pydantic import JsonValue

from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.llm.factory import get_llm_token_counter
from onyx.llm.interfaces import LLMUserIdentity
from onyx.llm.model_request import (
    AssistantMessage,
    ChatCompletionMessage,
    RequestFunctionCall,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from onyx.llm.model_response import ModelResponse, ModelResponseStream
from onyx.llm.models import (
    AnyThinkingBlock,
    ContentPart,
    ImageContentPart,
    ImageUrlDetail,
    NamedToolChoice,
    ReasoningEffort,
    RedactedThinkingBlock,
    TextContentPart,
    ThinkingBlock,
    ToolChoice,
    ToolChoiceOptions,
)
from onyx.llm.multi_llm import LitellmLLM
from onyx.server.gateway.models import (
    AnthropicContentBlock,
    AnthropicContentBlockDeltaEvent,
    AnthropicContentBlockStartEvent,
    AnthropicContentBlockStopEvent,
    AnthropicCountTokensRequest,
    AnthropicErrorEvent,
    AnthropicInputJsonDelta,
    AnthropicMessageDeltaEvent,
    AnthropicMessageDeltaPayload,
    AnthropicMessageResponse,
    AnthropicMessagesRequest,
    AnthropicMessageStartEvent,
    AnthropicMessageStopEvent,
    AnthropicPingEvent,
    AnthropicRedactedThinkingBlock,
    AnthropicSignatureDelta,
    AnthropicStreamEvent,
    AnthropicTextBlock,
    AnthropicTextDelta,
    AnthropicThinkingBlock,
    AnthropicThinkingDelta,
    AnthropicToolUseBlock,
    AnthropicUsagePayload,
)
from onyx.server.gateway.openai_compat import invalid_input, text_of
from onyx.server.gateway.stream_bridge import (
    StreamSink,
    run_bridged_stream,
    sse_event,
)
from onyx.server.gateway.translation import (
    TranslatedCall,
    gateway_llm_error,
    invoke_translated,
    reasoning_effort_from_name,
    stream_translated,
)
from onyx.tracing.flows import LLMFlow
from onyx.utils.logger import setup_logger

logger = setup_logger()

_STOP_REASONS: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

# Thinking budgets (tokens) at which a request reaches the next effort.
_LOW_BUDGET_MAX = 4096
_MEDIUM_BUDGET_MAX = 16384


def stop_reason(finish_reason: str | None) -> str:
    return _STOP_REASONS.get(finish_reason or "stop", "end_turn")


def _image_part(block: Mapping[str, Any]) -> ImageContentPart:
    source = block.get("source")
    if not isinstance(source, Mapping):
        raise invalid_input("An image block needs a source.")
    if source.get("type") == "base64":
        url = f"data:{source.get('media_type')};base64,{source.get('data')}"
    elif source.get("type") == "url" and isinstance(source.get("url"), str):
        url = source["url"]
    else:
        raise invalid_input("Only base64 and url image sources are supported.")
    return ImageContentPart(image_url=ImageUrlDetail(url=url))


def _tool_result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    text = text_of(content, field="tool_result")
    if block.get("is_error"):
        return f"Error: {text}"
    return text


def _user_turn(content: Any) -> list[ChatCompletionMessage]:
    if isinstance(content, str):
        return [UserMessage(content=content)]
    if not isinstance(content, list):
        raise invalid_input("Message content must be a string or a list of blocks.")
    # Tool results answer the previous assistant turn, so they come first.
    tool_messages: list[ChatCompletionMessage] = []
    parts: list[ContentPart] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise invalid_input("A content block must be an object.")
        block_type = block.get("type")
        if block_type == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                raise invalid_input("A tool_result block needs a tool_use_id.")
            tool_messages.append(
                ToolMessage(content=_tool_result_text(block), tool_call_id=tool_use_id)
            )
        elif block_type == "text" and isinstance(block.get("text"), str):
            parts.append(TextContentPart(text=block["text"]))
        elif block_type == "image":
            parts.append(_image_part(block))
        else:
            raise invalid_input(
                f"Content block type {block_type!r} is not supported for this model."
            )
    messages = tool_messages
    if parts:
        messages.append(UserMessage(content=parts))
    return messages


def _assistant_turn(content: Any) -> AssistantMessage:
    if isinstance(content, str):
        return AssistantMessage(content=content or None)
    if not isinstance(content, list):
        raise invalid_input("Message content must be a string or a list of blocks.")
    texts: list[str] = []
    calls: list[ToolCall] = []
    thinking: list[AnyThinkingBlock] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise invalid_input("A content block must be an object.")
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif block_type == "tool_use" and isinstance(block.get("id"), str):
            calls.append(
                ToolCall(
                    id=block["id"],
                    function=RequestFunctionCall(
                        name=str(block.get("name") or ""),
                        arguments=json.dumps(block.get("input") or {}),
                    ),
                )
            )
        elif block_type == "thinking":
            thinking.append(
                ThinkingBlock(
                    thinking=str(block.get("thinking") or ""),
                    signature=block.get("signature") or None,
                )
            )
        elif block_type == "redacted_thinking" and isinstance(block.get("data"), str):
            thinking.append(RedactedThinkingBlock(data=block["data"]))
        else:
            raise invalid_input(
                f"Content block type {block_type!r} is not supported for this model."
            )
    return AssistantMessage(
        content="".join(texts) or None,
        tool_calls=calls or None,
        thinking_blocks=thinking or None,
    )


def anthropic_messages_to_prompt(
    system: str | list[dict[str, Any]] | None, messages: list[dict[str, Any]]
) -> list[ChatCompletionMessage]:
    prompt: list[ChatCompletionMessage] = []
    if system:
        prompt.append(SystemMessage(content=text_of(system, field="system")))
    for message in messages:
        role = message.get("role")
        if role == "user":
            prompt.extend(_user_turn(message.get("content")))
        elif role == "assistant":
            prompt.append(_assistant_turn(message.get("content")))
        else:
            raise invalid_input(f"Unsupported message role: {role!r}.")
    return prompt


def anthropic_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, JsonValue]] | None:
    if not tools:
        return None
    converted: list[dict[str, JsonValue]] = []
    for tool in tools:
        tool_type = tool.get("type")
        if tool_type not in (None, "custom") or not isinstance(tool.get("name"), str):
            raise invalid_input(
                f"Tool type {tool_type!r} is not supported for this model. "
                "Only custom tools are supported."
            )
        function: dict[str, JsonValue] = {
            "name": tool["name"],
            "parameters": tool.get("input_schema") or {"type": "object"},
        }
        if tool.get("description"):
            function["description"] = tool["description"]
        converted.append({"type": "function", "function": function})
    return converted


def anthropic_tool_choice(value: dict[str, Any] | None) -> ToolChoice | None:
    if not value:
        return None
    choice_type = value.get("type")
    if choice_type == "auto":
        return ToolChoiceOptions.AUTO
    if choice_type == "any":
        return ToolChoiceOptions.REQUIRED
    if choice_type == "none":
        return ToolChoiceOptions.NONE
    if choice_type == "tool" and isinstance(value.get("name"), str):
        return NamedToolChoice(name=value["name"])
    raise invalid_input("Unsupported tool_choice.")


def anthropic_reasoning_effort(
    thinking: dict[str, Any] | None, output_config: dict[str, Any] | None
) -> ReasoningEffort:
    effort = output_config.get("effort") if output_config else None
    if isinstance(effort, str):
        return reasoning_effort_from_name(effort)
    if not thinking:
        return ReasoningEffort.AUTO
    thinking_type = thinking.get("type")
    if thinking_type == "disabled":
        return ReasoningEffort.OFF
    budget = thinking.get("budget_tokens")
    if thinking_type == "enabled" and isinstance(budget, int):
        if budget <= _LOW_BUDGET_MAX:
            return ReasoningEffort.LOW
        if budget <= _MEDIUM_BUDGET_MAX:
            return ReasoningEffort.MEDIUM
        return ReasoningEffort.HIGH
    return ReasoningEffort.AUTO


def build_messages_call(
    request: AnthropicMessagesRequest, user_id: str
) -> TranslatedCall:
    return TranslatedCall(
        prompt=anthropic_messages_to_prompt(request.system, request.messages),
        tools=anthropic_tools(request.tools),
        tool_choice=anthropic_tool_choice(request.tool_choice),
        max_tokens=request.max_tokens,
        reasoning_effort=anthropic_reasoning_effort(
            request.thinking, request.output_config
        ),
        user_identity=LLMUserIdentity(user_id=user_id),
    )


def _tool_input(arguments: str | None) -> dict[str, Any]:
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _thinking_content(
    blocks: list[AnyThinkingBlock] | None,
) -> list[AnthropicContentBlock]:
    content: list[AnthropicContentBlock] = []
    for block in blocks or []:
        if isinstance(block, ThinkingBlock) and block.signature:
            content.append(
                AnthropicThinkingBlock.create(
                    thinking=block.thinking, signature=block.signature
                )
            )
        elif isinstance(block, RedactedThinkingBlock):
            content.append(AnthropicRedactedThinkingBlock.create(data=block.data))
    return content


def message_response(response: ModelResponse, wire_id: str) -> AnthropicMessageResponse:
    message = response.choice.message
    content = _thinking_content(message.thinking_blocks)
    if message.content:
        content.append(AnthropicTextBlock.create(text=message.content))
    for call in message.tool_calls or []:
        content.append(
            AnthropicToolUseBlock.create(
                id=call.id,
                name=call.function.name or "",
                input=_tool_input(call.function.arguments),
            )
        )
    return AnthropicMessageResponse.from_parts(
        message_id=f"msg_{uuid4().hex}",
        model=wire_id,
        content=content,
        stop_reason=stop_reason(response.choice.finish_reason),
        usage=(
            AnthropicUsagePayload.from_usage(response.usage)
            if response.usage
            else AnthropicUsagePayload.zero()
        ),
    )


def messages_completion(
    llm: LitellmLLM, flow: LLMFlow, wire_id: str, call: TranslatedCall
) -> dict[str, Any]:
    response = invoke_translated(llm, flow, wire_id, call)
    return message_response(response, wire_id).to_wire()


def _wire_event(event: AnthropicStreamEvent) -> str:
    payload = event.to_wire()
    return sse_event(str(payload["type"]), payload)


def anthropic_error_type(error_code: OnyxErrorCode) -> str:
    if error_code is OnyxErrorCode.RATE_LIMITED:
        return "rate_limit_error"
    if error_code is OnyxErrorCode.INVALID_INPUT:
        return "invalid_request_error"
    if error_code is OnyxErrorCode.GATEWAY_TIMEOUT:
        return "timeout_error"
    return "api_error"


_BlockKind = Literal["thinking", "text", "tool_use"]


class _BlockWriter:
    """Opens and closes Anthropic content blocks as the stream changes kind."""

    def __init__(self, sink: StreamSink) -> None:
        self._sink = sink
        self._index = -1
        self._kind: _BlockKind | None = None

    def _start(self, kind: _BlockKind, block: AnthropicContentBlock) -> None:
        self.close()
        self._index += 1
        self._kind = kind
        self._sink.emit(
            _wire_event(
                AnthropicContentBlockStartEvent.create(
                    index=self._index, content_block=block
                )
            )
        )

    def close(self) -> None:
        if self._kind is None:
            return
        self._sink.emit(
            _wire_event(AnthropicContentBlockStopEvent.create(index=self._index))
        )
        self._kind = None

    def _delta(
        self,
        delta: AnthropicTextDelta
        | AnthropicThinkingDelta
        | AnthropicSignatureDelta
        | AnthropicInputJsonDelta,
    ) -> None:
        self._sink.emit(
            _wire_event(
                AnthropicContentBlockDeltaEvent.create(index=self._index, delta=delta)
            )
        )

    def thinking(self, text: str) -> None:
        if self._kind != "thinking":
            self._start(
                "thinking", AnthropicThinkingBlock.create(thinking="", signature="")
            )
        self._delta(AnthropicThinkingDelta.create(thinking=text))

    def signature(self, signature: str) -> None:
        if self._kind == "thinking":
            self._delta(AnthropicSignatureDelta.create(signature=signature))

    def redacted(self, data: str) -> None:
        self._start("thinking", AnthropicRedactedThinkingBlock.create(data=data))
        self.close()

    def text(self, text: str) -> None:
        if self._kind != "text":
            self._start("text", AnthropicTextBlock.create(text=""))
        self._delta(AnthropicTextDelta.create(text=text))

    def tool_use(self, tool_id: str, name: str) -> None:
        self._start(
            "tool_use", AnthropicToolUseBlock.create(id=tool_id, name=name, input={})
        )

    def tool_arguments(self, partial_json: str) -> None:
        if self._kind == "tool_use":
            self._delta(AnthropicInputJsonDelta.create(partial_json=partial_json))


def messages_stream(
    llm: LitellmLLM, flow: LLMFlow, wire_id: str, call: TranslatedCall
) -> Iterator[str]:
    message_id = f"msg_{uuid4().hex}"

    def produce(sink: StreamSink) -> None:
        sink.emit(
            _wire_event(
                AnthropicMessageStartEvent.create(
                    AnthropicMessageResponse.from_parts(
                        message_id=message_id,
                        model=wire_id,
                        content=[],
                        stop_reason=None,
                        usage=AnthropicUsagePayload.zero(),
                    )
                )
            )
        )
        sink.emit(_wire_event(AnthropicPingEvent.create()))
        blocks = _BlockWriter(sink)

        def on_chunk(chunk: ModelResponseStream) -> None:
            delta = chunk.choice.delta
            if delta.reasoning_content:
                blocks.thinking(delta.reasoning_content)
            for thinking_block in delta.thinking_blocks or []:
                if isinstance(thinking_block, ThinkingBlock):
                    if thinking_block.signature:
                        blocks.signature(thinking_block.signature)
                elif isinstance(thinking_block, RedactedThinkingBlock):
                    blocks.redacted(thinking_block.data)
            if delta.content:
                blocks.text(delta.content)
            for tool_delta in delta.tool_calls:
                if tool_delta.id:
                    name = tool_delta.function.name if tool_delta.function else None
                    blocks.tool_use(tool_delta.id, name or "")
                if tool_delta.function and tool_delta.function.arguments:
                    blocks.tool_arguments(tool_delta.function.arguments)

        summary = stream_translated(llm, flow, wire_id, call, on_chunk)
        blocks.close()
        sink.emit(
            _wire_event(
                AnthropicMessageDeltaEvent.create(
                    delta=AnthropicMessageDeltaPayload.create(
                        stop_reason=stop_reason(summary.finish_reason)
                    ),
                    usage=(
                        AnthropicUsagePayload.from_usage(summary.usage)
                        if summary.usage
                        else AnthropicUsagePayload.zero()
                    ),
                )
            )
        )
        sink.emit(_wire_event(AnthropicMessageStopEvent.create()))

    def error_frames(error: Exception) -> list[str]:
        onyx_error = gateway_llm_error(error, llm)
        return [
            _wire_event(
                AnthropicErrorEvent.create(
                    message=onyx_error.detail,
                    error_type=anthropic_error_type(onyx_error.error_code),
                )
            )
        ]

    return run_bridged_stream(produce, error_frames, name="gateway-messages-stream")


def estimate_input_tokens(llm: LitellmLLM, request: AnthropicCountTokensRequest) -> int:
    """A local estimate when the vendor cannot count. Never raises."""
    payload = json.dumps(
        {
            "system": request.system,
            "messages": request.messages,
            "tools": request.tools,
        },
        default=str,
    )
    try:
        return get_llm_token_counter(llm)(payload)
    except Exception as e:
        logger.warning("Gateway token estimate failed: %s", type(e).__name__)
        return max(1, len(payload) // 4)
