"""The parts both native passthroughs share: the upstream call, the SSE
relay, error forwarding and metering.

A passthrough bypasses ``LitellmLLM``, so it opens its own trace and span and
calls ``_track_llm_cost`` itself. It never sends a caller header upstream and
never returns the Onyx credential's own 401 or 403 to the caller.
"""

import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx
from fastapi import Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.llm.models import Usage
from onyx.llm.multi_llm import LitellmLLM
from onyx.server.gateway.stream_bridge import StreamSink, run_bridged_stream
from onyx.server.gateway.tracing import llm_gateway_trace
from onyx.tracing.flows import LLMFlow
from onyx.tracing.framework.span_data import GenerationSpanData
from onyx.tracing.framework.spans import Span
from onyx.tracing.llm_utils import llm_generation_span, record_llm_span_output
from onyx.utils.logger import setup_logger

logger = setup_logger()

# Upstream statuses that describe the caller's request or the vendor's load.
# 401 and 403 are left out: they describe the Onyx credential.
_FORWARDABLE_STATUSES = frozenset(
    {400, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504, 529}
)


class UpstreamRequest(BaseModel):
    """One vendor request. The headers are built fresh, never copied."""

    model_config = ConfigDict(frozen=True)

    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    connect_timeout_s: float
    read_timeout_s: float

    def client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(
                self.read_timeout_s,
                connect=self.connect_timeout_s,
            )
        )


class SseFrame(BaseModel):
    """One relayed SSE frame and its parsed data payload, when it has one."""

    model_config = ConfigDict(frozen=True)

    text: str
    event: str | None = None
    data: dict[str, Any] | None = None


class PassthroughObserver(BaseModel):
    """Collects usage and output text from the vendor payloads of one call."""

    usage: Usage | None = None
    text_parts: list[str] = Field(default_factory=list)

    def output_text(self) -> str | None:
        return "".join(self.text_parts) or None


def unreachable_upstream(error: httpx.HTTPError) -> OnyxError:
    # Only the type: the URL of a custom api_base can carry credentials.
    logger.warning("Gateway passthrough transport error: %s", type(error).__name__)
    return OnyxError(
        OnyxErrorCode.BAD_GATEWAY, "The model provider could not be reached."
    )


def upstream_failure(response: httpx.Response) -> Response:
    """The response for a vendor status other than 200."""
    if response.status_code in _FORWARDABLE_STATUSES:
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type="application/json",
        )
    logger.warning(
        "Gateway passthrough got status %d from the provider", response.status_code
    )
    raise OnyxError(
        OnyxErrorCode.BAD_GATEWAY,
        "The model provider rejected the gateway credential or failed.",
    )


def _set_transport_error(span: Span[GenerationSpanData], error: Exception) -> None:
    span.set_error({"message": type(error).__name__, "data": None})


def _finish_span(
    span: Span[GenerationSpanData], llm: LitellmLLM, observer: PassthroughObserver
) -> None:
    record_llm_span_output(span, output=observer.output_text(), usage=observer.usage)
    if observer.usage is not None:
        # The managed-key cost counter does not run outside LitellmLLM.
        llm._track_llm_cost(observer.usage)


def call_upstream_json(
    *,
    llm: LitellmLLM,
    flow: LLMFlow,
    wire_id: str,
    upstream: UpstreamRequest,
    input_messages: Any,
    observe: Callable[[dict[str, Any], PassthroughObserver], None],
) -> Response:
    """A non-streaming passthrough call inside the gateway trace and span."""
    observer = PassthroughObserver()
    with llm_gateway_trace(flow, wire_id):
        with llm_generation_span(
            llm=llm, flow=flow, input_messages=input_messages
        ) as span:
            try:
                with upstream.client() as client:
                    response = client.post(
                        upstream.url, headers=upstream.headers, json=upstream.body
                    )
            except httpx.HTTPError as e:
                _set_transport_error(span, e)
                raise unreachable_upstream(e) from e
            if response.status_code != httpx.codes.OK:
                span.set_error(
                    {
                        "message": "upstream error",
                        "data": {"status": response.status_code},
                    }
                )
                return upstream_failure(response)
            try:
                payload = response.json()
            except ValueError as e:
                _set_transport_error(span, e)
                raise OnyxError(
                    OnyxErrorCode.BAD_GATEWAY,
                    "The model provider returned a response that is not JSON.",
                ) from e
            if isinstance(payload, dict):
                observe(payload, observer)
            _finish_span(span, llm, observer)
            return JSONResponse(content=payload)


def parse_sse_frames(lines: Iterator[str]) -> Iterator[SseFrame]:
    """Group SSE lines into frames. Each frame keeps its original text."""
    buffer: list[str] = []
    for line in lines:
        if line:
            buffer.append(line)
            continue
        if buffer:
            yield _frame(buffer)
            buffer = []
    if buffer:
        yield _frame(buffer)


def _frame(lines: list[str]) -> SseFrame:
    event: str | None = None
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    data: dict[str, Any] | None = None
    if data_lines:
        try:
            parsed = json.loads("\n".join(data_lines))
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
    return SseFrame(text="\n".join(lines) + "\n\n", event=event, data=data)


def open_upstream_stream(
    upstream: UpstreamRequest,
) -> tuple[httpx.Client, httpx.Response] | Response:
    """Send the request before the HTTP status is committed, so a vendor
    rejection keeps its own status. The caller owns the returned client."""
    client = upstream.client()
    try:
        response = client.send(
            client.build_request(
                "POST", upstream.url, headers=upstream.headers, json=upstream.body
            ),
            stream=True,
        )
    except httpx.HTTPError as e:
        client.close()
        raise unreachable_upstream(e) from e
    if response.status_code != httpx.codes.OK:
        try:
            response.read()
        finally:
            response.close()
            client.close()
        return upstream_failure(response)
    return client, response


def relay_upstream_stream(
    *,
    llm: LitellmLLM,
    flow: LLMFlow,
    wire_id: str,
    client: httpx.Client,
    response: httpx.Response,
    input_messages: Any,
    observe: Callable[[SseFrame, PassthroughObserver], None],
    error_frames: Callable[[Exception], list[str]],
    name: str,
) -> Iterator[str]:
    """Relay the vendor's SSE frames unchanged from one worker thread."""

    def produce(sink: StreamSink) -> None:
        observer = PassthroughObserver()
        try:
            with llm_gateway_trace(flow, wire_id):
                with llm_generation_span(
                    llm=llm, flow=flow, input_messages=input_messages
                ) as span:
                    try:
                        for frame in parse_sse_frames(response.iter_lines()):
                            observe(frame, observer)
                            sink.emit(frame.text)
                    except httpx.HTTPError as e:
                        _set_transport_error(span, e)
                        raise unreachable_upstream(e) from e
                    finally:
                        _finish_span(span, llm, observer)
        finally:
            response.close()
            client.close()

    return run_bridged_stream(produce, error_frames, name=name)


def int_field(payload: Mapping[str, Any] | None, key: str) -> int:
    value = payload.get(key) if payload else None
    return value if isinstance(value, int) else 0
