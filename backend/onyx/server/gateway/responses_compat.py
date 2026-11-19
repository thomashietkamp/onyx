"""OpenAI Responses on the translated path.

Used for every model that is not a true OpenAI model, and for OpenAI models
when the passthrough is off. Hosted tools and encrypted reasoning need the
passthrough, so this path refuses hosted tools and drops reasoning items.
"""

import json
import time
from collections.abc import Iterator, Mapping
from typing import Any
from uuid import uuid4

from pydantic import JsonValue

from onyx.error_handling.error_codes import OnyxErrorCode
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
    ContentPart,
    ImageContentPart,
    ImageUrlDetail,
    NamedToolChoice,
    TextContentPart,
    ToolChoice,
    ToolChoiceOptions,
    Usage,
)
from onyx.llm.multi_llm import LitellmLLM
from onyx.server.gateway.models import (
    ResponsesCompletedEvent,
    ResponsesContentPartAddedEvent,
    ResponsesContentPartDoneEvent,
    ResponsesCreatedEvent,
    ResponsesErrorCode,
    ResponsesFailedEvent,
    ResponsesFunctionCallArgumentsDoneEvent,
    ResponsesFunctionCallItem,
    ResponsesMessageItem,
    ResponsesObjectPayload,
    ResponsesOutputItem,
    ResponsesOutputItemAddedEvent,
    ResponsesOutputItemDoneEvent,
    ResponsesOutputTextDeltaEvent,
    ResponsesOutputTextDoneEvent,
    ResponsesOutputTextPart,
    ResponsesRequest,
)
from onyx.server.gateway.openai_compat import invalid_input, text_of
from onyx.server.gateway.stream_bridge import (
    StreamSink,
    ToolCallAccumulator,
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


def _input_content(content: Any) -> str | list[ContentPart]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise invalid_input("Input message content must be a string or a list.")
    parts: list[ContentPart] = []
    for part in content:
        if not isinstance(part, Mapping):
            raise invalid_input("An input content part must be an object.")
        part_type = part.get("type")
        if part_type in ("input_text", "output_text", "text") and isinstance(
            part.get("text"), str
        ):
            parts.append(TextContentPart(text=part["text"]))
        elif part_type == "input_image" and isinstance(part.get("image_url"), str):
            detail = part.get("detail")
            parts.append(
                ImageContentPart(
                    image_url=ImageUrlDetail(
                        url=part["image_url"],
                        detail=detail if detail in ("auto", "low", "high") else None,
                    )
                )
            )
        else:
            raise invalid_input(f"Unsupported input content part type: {part_type!r}.")
    return parts


def _append_tool_call(prompt: list[ChatCompletionMessage], call: ToolCall) -> None:
    """Consecutive function_call items are one assistant turn."""
    last = prompt[-1] if prompt else None
    if isinstance(last, AssistantMessage):
        prompt[-1] = last.model_copy(
            update={"tool_calls": [*(last.tool_calls or []), call]}
        )
    else:
        prompt.append(AssistantMessage(tool_calls=[call]))


def responses_input_to_prompt(
    request_input: str | list[dict[str, Any]], instructions: str | None
) -> list[ChatCompletionMessage]:
    prompt: list[ChatCompletionMessage] = []
    if instructions:
        prompt.append(SystemMessage(content=instructions))
    if isinstance(request_input, str):
        prompt.append(UserMessage(content=request_input))
        return prompt
    for item in request_input:
        item_type = item.get("type", "message")
        if item_type == "message":
            role = item.get("role")
            content = item.get("content")
            if role in ("system", "developer"):
                prompt.append(SystemMessage(content=text_of(content, field=str(role))))
            elif role == "user":
                prompt.append(UserMessage(content=_input_content(content)))
            elif role == "assistant":
                prompt.append(
                    AssistantMessage(
                        content=text_of(content, field="assistant") or None
                    )
                )
            else:
                raise invalid_input(f"Unsupported input role: {role!r}.")
        elif item_type == "function_call":
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                raise invalid_input("A function_call item needs a call_id.")
            arguments = item.get("arguments")
            _append_tool_call(
                prompt,
                ToolCall(
                    id=call_id,
                    function=RequestFunctionCall(
                        name=str(item.get("name") or ""),
                        arguments=arguments
                        if isinstance(arguments, str)
                        else json.dumps(arguments or {}),
                    ),
                ),
            )
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                raise invalid_input("A function_call_output item needs a call_id.")
            output = item.get("output")
            prompt.append(
                ToolMessage(
                    content=text_of(output, field="function_call_output"),
                    tool_call_id=call_id,
                )
            )
        elif item_type == "reasoning":
            # Only the vendor that issued a reasoning item can read it.
            continue
        else:
            raise invalid_input(
                f"Input item type {item_type!r} is not supported for this model."
            )
    return prompt


def responses_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, JsonValue]] | None:
    if not tools:
        return None
    converted: list[dict[str, JsonValue]] = []
    for tool in tools:
        if tool.get("type") != "function" or not isinstance(tool.get("name"), str):
            raise invalid_input(
                f"Tool type {tool.get('type')!r} is not supported for this model. "
                "Only function tools are supported."
            )
        function: dict[str, JsonValue] = {"name": tool["name"]}
        for key in ("description", "parameters", "strict"):
            if tool.get(key) is not None:
                function[key] = tool[key]
        converted.append({"type": "function", "function": function})
    return converted


def responses_tool_choice(value: Any) -> ToolChoice | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return ToolChoiceOptions(value)
        except ValueError:
            raise invalid_input(f"Unsupported tool_choice: {value!r}.")
    if (
        isinstance(value, Mapping)
        and value.get("type") == "function"
        and isinstance(value.get("name"), str)
    ):
        return NamedToolChoice(name=value["name"])
    raise invalid_input("Unsupported tool_choice for this model.")


def _structured_format(request: ResponsesRequest) -> dict[str, JsonValue] | None:
    text = (request.model_extra or {}).get("text")
    text_format = text.get("format") if isinstance(text, Mapping) else None
    if not isinstance(text_format, Mapping):
        return None
    format_type = text_format.get("type")
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type == "json_schema":
        schema: dict[str, JsonValue] = {
            key: text_format[key]
            for key in ("name", "schema", "strict", "description")
            if key in text_format
        }
        return {"type": "json_schema", "json_schema": schema}
    return None


def build_responses_call(request: ResponsesRequest, user_id: str) -> TranslatedCall:
    effort = request.reasoning.get("effort") if request.reasoning else None
    return TranslatedCall(
        prompt=responses_input_to_prompt(request.input, request.instructions),
        tools=responses_tools(request.tools),
        tool_choice=responses_tool_choice(request.tool_choice),
        max_tokens=request.max_output_tokens,
        reasoning_effort=reasoning_effort_from_name(
            effort if isinstance(effort, str) else None
        ),
        structured_response_format=_structured_format(request),
        user_identity=LLMUserIdentity(user_id=user_id),
    )


def _status(finish_reason: str | None) -> str:
    return "incomplete" if finish_reason == "length" else "completed"


def _function_call_items(calls: list[ToolCall]) -> list[ResponsesFunctionCallItem]:
    return [
        ResponsesFunctionCallItem.create(
            id=f"fc_{uuid4().hex}",
            call_id=call.id,
            name=call.function.name,
            arguments=call.function.arguments,
        )
        for call in calls
    ]


def _response_output(
    content: str | None, calls: list[ToolCall], message_id: str
) -> list[ResponsesOutputItem]:
    output: list[ResponsesOutputItem] = []
    if content:
        output.append(
            ResponsesMessageItem.create(
                id=message_id,
                status="completed",
                content=[ResponsesOutputTextPart.create(text=content)],
            )
        )
    output.extend(_function_call_items(calls))
    return output


def _calls_of(response: ModelResponse) -> list[ToolCall]:
    return [
        ToolCall(
            id=call.id,
            function=RequestFunctionCall(
                name=call.function.name or "",
                arguments=call.function.arguments or "",
            ),
        )
        for call in response.choice.message.tool_calls or []
    ]


def responses_completion(
    llm: LitellmLLM, flow: LLMFlow, wire_id: str, call: TranslatedCall
) -> dict[str, Any]:
    response = invoke_translated(llm, flow, wire_id, call)
    return ResponsesObjectPayload.from_parts(
        response_id=f"resp_{uuid4().hex}",
        created_at=int(time.time()),
        model=wire_id,
        status=_status(response.choice.finish_reason),
        output=_response_output(
            response.choice.message.content, _calls_of(response), f"msg_{uuid4().hex}"
        ),
        usage=response.usage,
    ).to_wire()


def _wire_event(event: Any) -> str:
    payload: dict[str, Any] = event.to_wire()
    return sse_event(str(payload["type"]), payload)


def responses_stream(
    llm: LitellmLLM, flow: LLMFlow, wire_id: str, call: TranslatedCall
) -> Iterator[str]:
    response_id = f"resp_{uuid4().hex}"
    message_id = f"msg_{uuid4().hex}"
    created_at = int(time.time())

    def produce(sink: StreamSink) -> None:
        sink.emit(
            _wire_event(
                ResponsesCreatedEvent.create(
                    ResponsesObjectPayload.from_parts(
                        response_id=response_id,
                        created_at=created_at,
                        model=wire_id,
                        status="in_progress",
                        output=[],
                    )
                )
            )
        )
        text_started = False
        tool_calls = ToolCallAccumulator()

        def on_chunk(chunk: ModelResponseStream) -> None:
            nonlocal text_started
            for tool_delta in chunk.choice.delta.tool_calls:
                tool_calls.merge(tool_delta)
            text = chunk.choice.delta.content
            if not text:
                return
            if not text_started:
                text_started = True
                sink.emit(
                    _wire_event(
                        ResponsesOutputItemAddedEvent.create(
                            output_index=0,
                            item=ResponsesMessageItem.create(
                                id=message_id, status="in_progress", content=[]
                            ),
                        )
                    )
                )
                sink.emit(
                    _wire_event(
                        ResponsesContentPartAddedEvent.create(
                            item_id=message_id,
                            output_index=0,
                            part=ResponsesOutputTextPart.create(text=""),
                        )
                    )
                )
            sink.emit(
                _wire_event(
                    ResponsesOutputTextDeltaEvent.create(item_id=message_id, delta=text)
                )
            )

        summary = stream_translated(llm, flow, wire_id, call, on_chunk)
        output: list[ResponsesOutputItem] = []
        if text_started:
            part = ResponsesOutputTextPart.create(text=summary.content)
            message = ResponsesMessageItem.create(
                id=message_id, status="completed", content=[part]
            )
            sink.emit(
                _wire_event(
                    ResponsesOutputTextDoneEvent.create(
                        item_id=message_id, output_index=0, text=summary.content
                    )
                )
            )
            sink.emit(
                _wire_event(
                    ResponsesContentPartDoneEvent.create(
                        item_id=message_id, output_index=0, part=part
                    )
                )
            )
            sink.emit(
                _wire_event(
                    ResponsesOutputItemDoneEvent.create(output_index=0, item=message)
                )
            )
            output.append(message)
        for item in _function_call_items(tool_calls.tool_calls()):
            index = len(output)
            sink.emit(
                _wire_event(
                    ResponsesOutputItemAddedEvent.create(output_index=index, item=item)
                )
            )
            sink.emit(
                _wire_event(
                    ResponsesFunctionCallArgumentsDoneEvent.create(
                        item_id=item.id,
                        output_index=index,
                        arguments=item.arguments,
                        name=item.name,
                    )
                )
            )
            sink.emit(
                _wire_event(
                    ResponsesOutputItemDoneEvent.create(output_index=index, item=item)
                )
            )
            output.append(item)
        usage: Usage | None = summary.usage
        sink.emit(
            _wire_event(
                ResponsesCompletedEvent.create(
                    ResponsesObjectPayload.from_parts(
                        response_id=response_id,
                        created_at=created_at,
                        model=wire_id,
                        status=_status(summary.finish_reason),
                        output=output,
                        usage=usage,
                    )
                )
            )
        )

    def error_frames(error: Exception) -> list[str]:
        onyx_error = gateway_llm_error(error, llm)
        code: ResponsesErrorCode = (
            "rate_limit_exceeded"
            if onyx_error.error_code is OnyxErrorCode.RATE_LIMITED
            else "server_error"
        )
        return [
            _wire_event(
                ResponsesFailedEvent.create(
                    ResponsesObjectPayload.failed(
                        response_id=response_id,
                        created_at=created_at,
                        model=wire_id,
                        message=onyx_error.detail,
                        code=code,
                    )
                )
            )
        ]

    return run_bridged_stream(produce, error_frames, name="gateway-responses-stream")
