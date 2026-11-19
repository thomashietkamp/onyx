"""Request and response translation between the client dialects and LiteLLM."""

import json
from typing import Any

import pytest

from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.llm.model_request import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from onyx.llm.model_response import (
    ChatCompletionMessageToolCall,
    Choice,
    Message,
    ModelResponse,
    ResponseFunctionCall,
)
from onyx.llm.models import (
    NamedToolChoice,
    ReasoningEffort,
    ThinkingBlock,
    ToolChoiceOptions,
)
from onyx.server.gateway.anthropic_compat import (
    anthropic_messages_to_prompt,
    anthropic_reasoning_effort,
    anthropic_tool_choice,
    anthropic_tools,
    message_response,
    messages_stream,
)
from onyx.server.gateway.models import ChatCompletionRequest, ResponsesRequest
from onyx.server.gateway.openai_compat import (
    build_chat_call,
    chat_completion,
    chat_completion_stream,
    chat_messages_to_prompt,
)
from onyx.server.gateway.responses_compat import (
    build_responses_call,
    responses_input_to_prompt,
    responses_stream,
    responses_tools,
)
from onyx.server.gateway.translation import TranslatedCall
from onyx.tracing.flows import LLMFlow
from tests.unit.onyx.server.gateway.conftest import (
    USAGE,
    SpanRecorder,
    fake_llm,
    stream_chunk,
)

_WIRE_ID = "7/test-model"


def _events(frames: list[str]) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for frame in frames
        for line in frame.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def test_chat_messages_translate_roles_tools_and_images() -> None:
    prompt = chat_messages_to_prompt(
        [
            {"role": "developer", "content": [{"type": "text", "text": "Be brief."}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "look", "arguments": '{"a":1}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "a cat"},
        ]
    )
    assert isinstance(prompt[0], SystemMessage)
    assert prompt[0].content == "Be brief."
    assert isinstance(prompt[1], UserMessage)
    assert isinstance(prompt[1].content, list) and len(prompt[1].content) == 2
    assert isinstance(prompt[2], AssistantMessage)
    assert prompt[2].tool_calls is not None
    assert prompt[2].tool_calls[0].function.arguments == '{"a":1}'
    assert isinstance(prompt[3], ToolMessage)
    assert prompt[3].tool_call_id == "call_1"


def test_chat_unknown_role_is_invalid_input() -> None:
    with pytest.raises(OnyxError) as error:
        chat_messages_to_prompt([{"role": "narrator", "content": "x"}])
    assert error.value.error_code is OnyxErrorCode.INVALID_INPUT


def test_chat_call_maps_tool_choice_effort_and_max_tokens() -> None:
    call = build_chat_call(
        ChatCompletionRequest(
            model=_WIRE_ID,
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
            tool_choice={"type": "function", "function": {"name": "f"}},
            max_completion_tokens=50,
            reasoning_effort="minimal",
            reasoningSummary="auto",
        ),
        user_id="hash",
    )
    assert call.tool_choice == NamedToolChoice(name="f")
    assert call.max_tokens == 50
    assert call.reasoning_effort is ReasoningEffort.LOW
    assert call.user_identity is not None and call.user_identity.user_id == "hash"


def test_chat_completion_records_span_with_flow(span_recorder: SpanRecorder) -> None:
    llm = fake_llm()
    llm.invoke_raw.return_value = ModelResponse(
        id="r1",
        created="1700000000",
        choice=Choice(
            finish_reason="stop", message=Message(content="Hello", role="assistant")
        ),
        usage=USAGE,
    )
    body = chat_completion(
        llm,
        LLMFlow.LLM_GATEWAY,
        _WIRE_ID,
        TranslatedCall(prompt=[UserMessage(content="hi")]),
    )
    assert body["model"] == _WIRE_ID
    assert body["choices"][0]["message"]["content"] == "Hello"
    assert body["usage"]["total_tokens"] == 15
    assert span_recorder.flows == [LLMFlow.LLM_GATEWAY]


def test_chat_stream_frames_end_with_done_and_record_usage(
    span_recorder: SpanRecorder,
) -> None:
    llm = fake_llm()
    llm.stream_raw.return_value = iter(
        [
            stream_chunk(content="Hel"),
            stream_chunk(content="lo", finish_reason="stop"),
            stream_chunk(usage=USAGE),
        ]
    )
    frames = list(
        chat_completion_stream(
            llm,
            LLMFlow.CRAFT_LLM_GENERATION,
            _WIRE_ID,
            TranslatedCall(prompt=[UserMessage(content="hi")]),
        )
    )
    assert frames[-1] == "data: [DONE]\n\n"
    events = _events(frames)
    assert {event["id"] for event in events} == {events[0]["id"]}
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "role" not in events[1]["choices"][0]["delta"]
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
    assert text == "Hello"
    assert span_recorder.flows == [LLMFlow.CRAFT_LLM_GENERATION]
    assert span_recorder.spans[0].span_data.usage["output_tokens"] == 5


def test_chat_stream_error_is_in_band(span_recorder: SpanRecorder) -> None:
    llm = fake_llm()

    def failing_stream(**_: Any) -> Any:
        yield stream_chunk(content="partial")
        raise OnyxError(OnyxErrorCode.LLM_PROVIDER_ERROR, "provider broke")

    llm.stream_raw.side_effect = failing_stream
    frames = list(
        chat_completion_stream(
            llm,
            LLMFlow.LLM_GATEWAY,
            _WIRE_ID,
            TranslatedCall(prompt=[UserMessage(content="hi")]),
        )
    )
    assert "data: [DONE]\n\n" not in frames
    assert _events(frames)[-1]["error"]["message"] == "provider broke"
    span_recorder.spans[0].set_error.assert_called_once()


def test_anthropic_tool_results_come_before_user_text() -> None:
    prompt = anthropic_messages_to_prompt(
        [{"type": "text", "text": "System text"}],
        [
            {"role": "user", "content": "List files"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {"type": "text", "text": "Running ls."},
                    {"type": "tool_use", "id": "tu_1", "name": "ls", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "And then?"},
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "a b"},
                ],
            },
        ],
    )
    assert isinstance(prompt[0], SystemMessage)
    assistant = prompt[2]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.thinking_blocks == [ThinkingBlock(thinking="hmm", signature="sig")]
    assert assistant.tool_calls is not None
    assert assistant.tool_calls[0].function.name == "ls"
    assert isinstance(prompt[3], ToolMessage)
    assert isinstance(prompt[4], UserMessage)


def test_anthropic_server_tool_needs_passthrough() -> None:
    with pytest.raises(OnyxError) as error:
        anthropic_tools([{"type": "web_search_20250305", "name": "web_search"}])
    assert error.value.error_code is OnyxErrorCode.INVALID_INPUT


def test_anthropic_tool_choice_and_thinking_mapping() -> None:
    assert anthropic_tool_choice({"type": "any"}) is ToolChoiceOptions.REQUIRED
    assert anthropic_tool_choice({"type": "tool", "name": "x"}) == NamedToolChoice(
        name="x"
    )
    assert anthropic_reasoning_effort({"type": "disabled"}, None) is (
        ReasoningEffort.OFF
    )
    assert (
        anthropic_reasoning_effort({"type": "enabled", "budget_tokens": 2048}, None)
        is ReasoningEffort.LOW
    )
    assert (
        anthropic_reasoning_effort({"type": "adaptive"}, {"effort": "max"})
        is ReasoningEffort.XHIGH
    )


def test_anthropic_response_has_tool_use_and_exclusive_input_tokens() -> None:
    response = ModelResponse(
        id="r1",
        created="1",
        choice=Choice(
            finish_reason="tool_calls",
            message=Message(
                content="Checking.",
                tool_calls=[
                    ChatCompletionMessageToolCall(
                        id="tu_1",
                        function=ResponseFunctionCall(
                            name="ls", arguments='{"path": "."}'
                        ),
                    )
                ],
            ),
        ),
        usage=USAGE,
    )
    body = message_response(response, _WIRE_ID).to_wire()
    assert body["stop_reason"] == "tool_use"
    assert body["stop_sequence"] is None
    assert [block["type"] for block in body["content"]] == ["text", "tool_use"]
    assert body["content"][1]["input"] == {"path": "."}
    # 10 prompt tokens include 2 cache reads, which Anthropic counts apart.
    assert body["usage"]["input_tokens"] == 8


@pytest.mark.usefixtures("span_recorder")
def test_anthropic_stream_event_order() -> None:
    llm = fake_llm()
    llm.stream_raw.return_value = iter(
        [
            stream_chunk(reasoning="think"),
            stream_chunk(content="Hi"),
            stream_chunk(tool_id="tu_1", tool_name="ls", tool_arguments='{"a"'),
            stream_chunk(tool_arguments=":1}", finish_reason="tool_calls"),
            stream_chunk(usage=USAGE),
        ]
    )
    frames = list(
        messages_stream(
            llm,
            LLMFlow.LLM_GATEWAY,
            _WIRE_ID,
            TranslatedCall(prompt=[UserMessage(content="hi")]),
        )
    )
    types = [event["type"] for event in _events(frames)]
    assert types == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    message_delta = _events(frames)[-2]
    assert message_delta["delta"]["stop_reason"] == "tool_use"
    assert message_delta["usage"]["output_tokens"] == 5
    assert all(frame.startswith("event: ") for frame in frames)


def test_responses_input_translation() -> None:
    prompt = responses_input_to_prompt(
        [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "x"},
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
            {"type": "function_call", "call_id": "c2", "name": "g", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
        ],
        instructions="Be kind.",
    )
    assert isinstance(prompt[0], SystemMessage)
    assert isinstance(prompt[1], UserMessage)
    assistant = prompt[2]
    assert isinstance(assistant, AssistantMessage)
    assert [call.id for call in assistant.tool_calls or []] == ["c1", "c2"]
    assert isinstance(prompt[3], ToolMessage)


def test_responses_hosted_tool_needs_passthrough() -> None:
    with pytest.raises(OnyxError) as error:
        responses_tools([{"type": "web_search"}])
    assert error.value.error_code is OnyxErrorCode.INVALID_INPUT


def test_responses_call_maps_json_schema_format() -> None:
    call = build_responses_call(
        ResponsesRequest(
            model=_WIRE_ID,
            input="hi",
            reasoning={"effort": "high"},
            text={"format": {"type": "json_schema", "name": "n", "schema": {}}},
        ),
        user_id="hash",
    )
    assert call.reasoning_effort is ReasoningEffort.HIGH
    assert call.structured_response_format == {
        "type": "json_schema",
        "json_schema": {"name": "n", "schema": {}},
    }


@pytest.mark.usefixtures("span_recorder")
def test_responses_stream_completes_with_usage() -> None:
    llm = fake_llm()
    llm.stream_raw.return_value = iter(
        [
            stream_chunk(content="4"),
            stream_chunk(tool_id="c1", tool_name="f", tool_arguments="{}"),
            stream_chunk(finish_reason="stop", usage=USAGE),
        ]
    )
    frames = list(
        responses_stream(
            llm,
            LLMFlow.LLM_GATEWAY,
            _WIRE_ID,
            TranslatedCall(prompt=[UserMessage(content="hi")]),
        )
    )
    events = _events(frames)
    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.completed"
    completed = events[-1]["response"]
    assert [item["type"] for item in completed["output"]] == [
        "message",
        "function_call",
    ]
    assert completed["output"][0]["content"][0]["text"] == "4"
    assert completed["usage"]["total_tokens"] == 15
