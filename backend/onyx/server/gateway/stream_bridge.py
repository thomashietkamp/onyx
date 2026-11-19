"""Run a gateway stream on one worker thread and hand its SSE frames to Starlette.

Starlette resumes a sync generator on different threadpool threads, and a
span's ContextVar token cannot reset across contexts. So one worker thread
owns the trace, the span and the upstream stream from start to end, and the
response generator only reads frames from a queue.
"""

import json
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from onyx.llm.model_request import RequestFunctionCall, ToolCall
from onyx.llm.model_response import ChatCompletionDeltaToolCall
from onyx.utils.logger import setup_logger
from onyx.utils.threadpool_concurrency import start_thread_with_context

logger = setup_logger()

# The gap the response generator waits for one frame before it checks that the
# worker is still alive. It does not limit the stream.
_FRAME_POLL_SECONDS = 5.0

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Tells nginx to send each frame at once instead of buffering the stream.
    "X-Accel-Buffering": "no",
}


class StreamCancelled(Exception):
    """The client went away. The worker stops reading the upstream stream."""


class StreamSink:
    """The worker's handle: it emits SSE frames and sees cancellation."""

    def __init__(self) -> None:
        self._frames: queue.Queue[str | None] = queue.Queue()
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def emit(self, frame: str) -> None:
        if self._cancelled.is_set():
            raise StreamCancelled()
        self._frames.put(frame)

    def _close(self) -> None:
        self._frames.put(None)

    def _cancel(self) -> None:
        self._cancelled.set()


def sse_data(payload: dict[str, Any]) -> str:
    """An SSE frame with only a data line (OpenAI Chat Completions)."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def sse_event(event_type: str, payload: dict[str, Any]) -> str:
    """An SSE frame with an event line (Responses, Anthropic Messages)."""
    return (
        f"event: {event_type}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )


def _guarded_worker(
    sink: StreamSink,
    produce: Callable[[StreamSink], None],
    error_frames: Callable[[Exception], list[str]],
) -> None:
    """The one teardown point of a stream worker.

    ``produce`` owns the trace, span and upstream. This guard turns an error
    into the dialect's in-band error frames, because the HTTP status was
    already 200, and always ends the frame queue.
    """
    try:
        produce(sink)
    except StreamCancelled:
        logger.debug("Gateway stream cancelled by the client")
    except Exception as e:
        logger.warning("Gateway stream failed: %s", type(e).__name__)
        try:
            for frame in error_frames(e):
                sink.emit(frame)
        except StreamCancelled:
            pass
    finally:
        sink._close()


def run_bridged_stream(
    produce: Callable[[StreamSink], None],
    error_frames: Callable[[Exception], list[str]],
    name: str,
) -> Iterator[str]:
    """Start the worker now and return the generator of its frames.

    The worker starts at once with a copy of the request context (tenant,
    user and Craft session), so it ends and closes its upstream even when the
    client never reads the body.
    """
    sink = StreamSink()
    thread = start_thread_with_context(
        _guarded_worker,
        name=name,
        daemon=True,
        args=(sink, produce, error_frames),
    )

    def frames() -> Iterator[str]:
        try:
            while True:
                try:
                    frame = sink._frames.get(timeout=_FRAME_POLL_SECONDS)
                except queue.Empty:
                    if not thread.is_alive() and sink._frames.empty():
                        return
                    continue
                if frame is None:
                    return
                yield frame
        finally:
            sink._cancel()

    return frames()


def streaming_response(frames: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(
        frames, media_type="text/event-stream", headers=SSE_HEADERS
    )


class _PartialToolCall(BaseModel):
    id: str = ""
    name: str = ""
    arguments: str = ""


class ToolCallAccumulator:
    """Builds complete tool calls from stream fragments, keyed by ``index``.

    The first fragment of an index has the id and the name. Later fragments
    carry only argument text.
    """

    def __init__(self) -> None:
        self._calls: dict[int, _PartialToolCall] = {}

    def merge(self, delta: ChatCompletionDeltaToolCall) -> None:
        call = self._calls.setdefault(delta.index, _PartialToolCall())
        if delta.id:
            call.id = delta.id
        if delta.function is not None:
            if delta.function.name:
                call.name = delta.function.name
            if delta.function.arguments:
                call.arguments += delta.function.arguments

    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(
                id=call.id,
                function=RequestFunctionCall(name=call.name, arguments=call.arguments),
            )
            for _, call in sorted(self._calls.items())
        ]
