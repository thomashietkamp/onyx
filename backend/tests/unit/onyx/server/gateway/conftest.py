"""Shared fakes for the gateway unit tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from onyx.llm.model_response import (
    ChatCompletionDeltaToolCall,
    Delta,
    ModelResponseStream,
    ResponseFunctionCall,
    StreamingChoice,
)
from onyx.llm.models import Usage
from onyx.llm.multi_llm import LitellmLLM
from onyx.tracing.flows import LLMFlow

USAGE = Usage(
    prompt_tokens=10,
    completion_tokens=5,
    total_tokens=15,
    cache_creation_input_tokens=0,
    cache_read_input_tokens=2,
)


def stream_chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_id: str | None = None,
    tool_name: str | None = None,
    tool_arguments: str | None = None,
    finish_reason: str | None = None,
    usage: Usage | None = None,
) -> ModelResponseStream:
    tool_calls: list[ChatCompletionDeltaToolCall] = []
    if tool_id or tool_name or tool_arguments:
        tool_calls.append(
            ChatCompletionDeltaToolCall(
                id=tool_id,
                index=0,
                function=ResponseFunctionCall(name=tool_name, arguments=tool_arguments),
            )
        )
    return ModelResponseStream(
        id="provider-id",
        created="1700000000",
        choice=StreamingChoice(
            finish_reason=finish_reason,
            delta=Delta(
                content=content, reasoning_content=reasoning, tool_calls=tool_calls
            ),
        ),
        usage=usage,
    )


def fake_llm() -> MagicMock:
    llm = MagicMock(spec=LitellmLLM)
    llm.config = MagicMock()
    return llm


class SpanRecorder:
    """Stands in for llm_generation_span and keeps each opened span."""

    def __init__(self) -> None:
        self.flows: list[LLMFlow | None] = []
        self.spans: list[MagicMock] = []

    @contextmanager
    def __call__(
        self,
        llm: Any,  # noqa: ARG002 - matches llm_generation_span
        flow: LLMFlow | None,
        **_: Any,
    ) -> Iterator[MagicMock]:
        span = MagicMock()
        span.span_data.usage = None
        self.flows.append(flow)
        self.spans.append(span)
        yield span


@pytest.fixture
def span_recorder() -> Iterator[SpanRecorder]:
    recorder = SpanRecorder()
    with (
        patch("onyx.server.gateway.translation.llm_generation_span", recorder),
        patch("onyx.server.gateway.passthrough.llm_generation_span", recorder),
    ):
        yield recorder
