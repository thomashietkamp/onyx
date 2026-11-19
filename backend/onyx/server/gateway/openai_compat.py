"""OpenAI Chat Completions on the translated path."""

import json
import time
from collections.abc import Iterator, Mapping
from typing import Any
from uuid import uuid4

from pydantic import JsonValue

from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
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
from onyx.llm.model_response import ModelResponseStream
from onyx.llm.models import (
    ContentPart,
    ImageContentPart,
    ImageUrlDetail,
    NamedToolChoice,
    TextContentPart,
    ToolChoice,
    ToolChoiceOptions,
)
from onyx.llm.multi_llm import LitellmLLM
from onyx.server.gateway.models import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from onyx.server.gateway.stream_bridge import (
    StreamSink,
    run_bridged_stream,
    sse_data,
)
from onyx.server.gateway.translation import (
    TranslatedCall,
    gateway_llm_error,
    invoke_translated,
    reasoning_effort_from_name,
    stream_translated,
)
from onyx.tracing.flows import LLMFlow


def invalid_input(message: str) -> OnyxError:
    return OnyxError(OnyxErrorCode.INVALID_INPUT, message)


def text_of(content: Any, *, field: str) -> str:
    """Plain text from a string or a list of text parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if not isinstance(part, Mapping):
                raise invalid_input(
                    f"{field} has a content part that is not an object."
                )
            text = part.get("text")
            if not isinstance(text, str):
                raise invalid_input(f"{field} accepts only text content parts.")
            texts.append(text)
        return "\n".join(texts)
    raise invalid_input(f"{field} content must be a string or a list of parts.")


def _user_content(content: Any) -> str | list[ContentPart]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise invalid_input("User message content must be a string or a list.")
    parts: list[ContentPart] = []
    for part in content:
        if not isinstance(part, Mapping):
            raise invalid_input("A user content part must be an object.")
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            parts.append(TextContentPart(text=part["text"]))
        elif part_type == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, Mapping) else image
            if not isinstance(url, str):
                raise invalid_input("An image_url part needs a url.")
            detail = image.get("detail") if isinstance(image, Mapping) else None
            parts.append(
                ImageContentPart(
                    image_url=ImageUrlDetail(
                        url=url,
                        detail=detail if detail in ("auto", "low", "high") else None,
                    )
                )
            )
        else:
            raise invalid_input(f"Unsupported user content part type: {part_type!r}.")
    return parts


def _tool_calls(raw: Any) -> list[ToolCall] | None:
    if not raw:
        return None
    if not isinstance(raw, list):
        raise invalid_input("tool_calls must be a list.")
    calls: list[ToolCall] = []
    for item in raw:
        function = item.get("function") if isinstance(item, Mapping) else None
        if not isinstance(function, Mapping) or not isinstance(item.get("id"), str):
            raise invalid_input("Each tool call needs an id and a function.")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {})
        calls.append(
            ToolCall(
                id=item["id"],
                function=RequestFunctionCall(
                    name=str(function.get("name") or ""), arguments=arguments
                ),
            )
        )
    return calls


def chat_messages_to_prompt(
    messages: list[dict[str, Any]],
) -> list[ChatCompletionMessage]:
    prompt: list[ChatCompletionMessage] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in ("system", "developer"):
            prompt.append(SystemMessage(content=text_of(content, field=str(role))))
        elif role == "user":
            prompt.append(UserMessage(content=_user_content(content)))
        elif role == "assistant":
            text = text_of(content, field="assistant") if content is not None else None
            prompt.append(
                AssistantMessage(
                    content=text or None,
                    tool_calls=_tool_calls(message.get("tool_calls")),
                )
            )
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str):
                raise invalid_input("A tool message needs a tool_call_id.")
            prompt.append(
                ToolMessage(
                    content=text_of(content, field="tool"), tool_call_id=tool_call_id
                )
            )
        else:
            raise invalid_input(f"Unsupported message role: {role!r}.")
    return prompt


def chat_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, JsonValue]] | None:
    if not tools:
        return None
    converted: list[dict[str, JsonValue]] = []
    for tool in tools:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, Mapping):
            raise invalid_input("Only function tools are supported.")
        converted.append({"type": "function", "function": dict(function)})
    return converted


def chat_tool_choice(value: Any) -> ToolChoice | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return ToolChoiceOptions(value)
        except ValueError:
            raise invalid_input(f"Unsupported tool_choice: {value!r}.")
    if isinstance(value, Mapping):
        function = value.get("function")
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            return NamedToolChoice(name=function["name"])
    raise invalid_input("Unsupported tool_choice.")


def _structured_format(
    response_format: dict[str, Any] | None,
) -> dict[str, JsonValue] | None:
    if not response_format or response_format.get("type") == "text":
        return None
    return dict(response_format)


def build_chat_call(request: ChatCompletionRequest, user_id: str) -> TranslatedCall:
    return TranslatedCall(
        prompt=chat_messages_to_prompt(request.messages),
        tools=chat_tools(request.tools),
        tool_choice=chat_tool_choice(request.tool_choice),
        max_tokens=request.max_completion_tokens or request.max_tokens,
        reasoning_effort=reasoning_effort_from_name(request.reasoning_effort),
        structured_response_format=_structured_format(request.response_format),
        user_identity=LLMUserIdentity(user_id=user_id),
    )


def chat_completion(
    llm: LitellmLLM, flow: LLMFlow, wire_id: str, call: TranslatedCall
) -> dict[str, Any]:
    response = invoke_translated(llm, flow, wire_id, call)
    return ChatCompletionResponse.from_model_response(response, model=wire_id).to_wire()


def _error_frames(error: Exception, llm: LitellmLLM) -> list[str]:
    onyx_error = gateway_llm_error(error, llm)
    return [
        sse_data(
            {
                "error": {
                    "message": onyx_error.detail,
                    "type": "server_error",
                    "code": onyx_error.error_code.code,
                }
            }
        )
    ]


def chat_completion_stream(
    llm: LitellmLLM, flow: LLMFlow, wire_id: str, call: TranslatedCall
) -> Iterator[str]:
    completion_id = f"chatcmpl-{uuid4().hex}"
    created = str(int(time.time()))

    def produce(sink: StreamSink) -> None:
        sent_role = False

        def on_chunk(chunk: ModelResponseStream) -> None:
            nonlocal sent_role
            # One id for the whole stream, as the OpenAI wire contract has it.
            chunk = chunk.model_copy(update={"id": completion_id, "created": created})
            payload = ChatCompletionChunk.from_stream_chunk(
                chunk, model=wire_id, include_role=not sent_role
            )
            sent_role = True
            sink.emit(sse_data(payload.to_wire()))

        stream_translated(llm, flow, wire_id, call, on_chunk)
        sink.emit("data: [DONE]\n\n")

    return run_bridged_stream(
        produce,
        lambda error: _error_frames(error, llm),
        name="gateway-chat-completions-stream",
    )
