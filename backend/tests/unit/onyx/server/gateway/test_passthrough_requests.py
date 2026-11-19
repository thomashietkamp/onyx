"""Passthrough request building: credentials, shared vendor state, metering."""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.gateway import anthropic_passthrough, openai_passthrough
from onyx.server.gateway.access import ResolvedGatewayModel
from onyx.server.gateway.passthrough import (
    UpstreamRequest,
    call_upstream_json,
    parse_sse_frames,
)
from onyx.server.manage.llm.models import LLMProviderView, ModelConfigurationView
from onyx.tracing.flows import LLMFlow
from tests.unit.onyx.server.gateway.conftest import SpanRecorder, fake_llm


def _resolved(provider_type: str, model_name: str) -> ResolvedGatewayModel:
    model = ModelConfigurationView(
        id=3, name=model_name, is_visible=True, supports_image_input=False
    )
    provider = LLMProviderView(
        id=7,
        name="p",
        provider=provider_type,
        api_key="sk-onyx",
        model_configurations=[model],
    )
    return ResolvedGatewayModel(
        wire_id=f"7/{model_name}", provider=provider, model=model
    )


def test_anthropic_headers_use_the_onyx_key_and_allowlisted_betas() -> None:
    headers = anthropic_passthrough.build_upstream_headers(
        "sk-onyx",
        {
            "authorization": "Bearer onyx_pat_caller",
            "x-api-key": "caller-key",
            "anthropic-beta": "interleaved-thinking-2025-05-14,mcp-client-2025-04-04",
        },
    )
    assert headers["x-api-key"] == "sk-onyx"
    assert "authorization" not in headers
    assert headers["anthropic-beta"] == "interleaved-thinking-2025-05-14"
    assert headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    "body",
    [
        {"mcp_servers": [{"type": "url", "url": "https://example.com/mcp"}]},
        {"container": "container_123"},
        {"tools": [{"type": "web_fetch_20250910", "name": "web_fetch"}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": "f"},
                        }
                    ],
                }
            ]
        },
    ],
)
def test_anthropic_refuses_shared_state_and_caller_urls(body: dict[str, Any]) -> None:
    with pytest.raises(OnyxError) as error:
        anthropic_passthrough.build_upstream_body(
            {"messages": [], "max_tokens": 1, **body}, "claude-haiku-4-5", "hash"
        )
    assert error.value.error_code is OnyxErrorCode.INVALID_INPUT


def test_anthropic_body_hashes_identity_and_keeps_server_tools() -> None:
    upstream = anthropic_passthrough.build_upstream_body(
        {
            "model": "7/claude-haiku-4-5",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"user_id": "someone-else"},
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        },
        "claude-haiku-4-5",
        "onyx-hash",
    )
    assert upstream["model"] == "claude-haiku-4-5"
    assert upstream["metadata"] == {"user_id": "onyx-hash"}
    assert upstream["tools"][0]["type"] == "web_search_20250305"


def test_anthropic_usage_counts_cache_in_prompt_tokens() -> None:
    usage = anthropic_passthrough.anthropic_usage(
        {
            "input_tokens": 5,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
            "output_tokens": 4,
        }
    )
    assert usage.prompt_tokens == 10
    assert usage.total_tokens == 14


def test_openai_body_is_stateless_and_hashes_identity() -> None:
    upstream = openai_passthrough.build_upstream_body(
        {
            "model": "7/gpt-6-luna",
            "input": "hi",
            "store": True,
            "user": "caller",
            "metadata": {"a": "b"},
            "prompt_cache_key": "k",
        },
        "gpt-6-luna",
        "onyx-hash",
    )
    assert upstream["store"] is False
    assert upstream["model"] == "gpt-6-luna"
    assert "user" not in upstream and "metadata" not in upstream
    assert upstream["safety_identifier"] == "onyx-hash"
    assert upstream["prompt_cache_key"] == "onyx-hash:k"


def test_openai_previous_response_id_is_not_implemented() -> None:
    with pytest.raises(OnyxError) as error:
        openai_passthrough.build_upstream_body(
            {"input": "hi", "previous_response_id": "resp_1"}, "gpt-6-luna", "h"
        )
    assert error.value.error_code is OnyxErrorCode.NOT_IMPLEMENTED


@pytest.mark.parametrize(
    "body",
    [
        {"tools": [{"type": "mcp", "server_url": "https://example.com/mcp"}]},
        {"tools": [{"type": "file_search", "vector_store_ids": ["vs_1"]}]},
        {"conversation": "conv_1"},
        {"input": [{"type": "item_reference", "id": "msg_1"}]},
        {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_file", "file_id": "file_1"}],
                }
            ]
        },
    ],
)
def test_openai_refuses_shared_state_and_caller_urls(body: dict[str, Any]) -> None:
    with pytest.raises(OnyxError) as error:
        openai_passthrough.build_upstream_body(
            {"input": "hi", **body}, "gpt-6-luna", "h"
        )
    assert error.value.error_code is OnyxErrorCode.INVALID_INPUT


def test_openai_headers_carry_only_the_onyx_credential() -> None:
    assert openai_passthrough.build_upstream_headers("sk-onyx") == {
        "Authorization": "Bearer sk-onyx",
        "Content-Type": "application/json",
    }


def test_eligibility_follows_provider_type_and_kill_switches() -> None:
    anthropic = _resolved("anthropic", "claude-haiku-4-5")
    assert anthropic_passthrough.is_anthropic_passthrough_eligible(anthropic)
    with patch(
        "onyx.server.gateway.configs.ANTHROPIC_GATEWAY_PASSTHROUGH_ENABLED", False
    ):
        assert not anthropic_passthrough.is_anthropic_passthrough_eligible(anthropic)
    assert not openai_passthrough.is_openai_passthrough_eligible(anthropic)
    assert not openai_passthrough.is_openai_passthrough_eligible(
        _resolved("openai", "my-local-llama")
    )


def test_sse_frames_keep_text_and_parse_data() -> None:
    frames = list(
        parse_sse_frames(
            iter(
                [
                    "event: message_start",
                    'data: {"type": "message_start"}',
                    "",
                    ": keep-alive",
                    "",
                ]
            )
        )
    )
    assert frames[0].event == "message_start"
    assert frames[0].data == {"type": "message_start"}
    assert frames[0].text == 'event: message_start\ndata: {"type": "message_start"}\n\n'
    assert frames[1].data is None


def _upstream() -> UpstreamRequest:
    return UpstreamRequest(
        url="https://vendor.test/v1/messages",
        headers={"x-api-key": "sk-onyx"},
        body={"model": "m"},
        connect_timeout_s=1,
        read_timeout_s=1,
    )


def _patched_client(response: httpx.Response) -> Any:
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = response
    return patch.object(UpstreamRequest, "client", return_value=client)


def test_passthrough_meters_managed_cost_and_span(span_recorder: SpanRecorder) -> None:
    llm = fake_llm()
    response = httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    )
    with _patched_client(response):
        result = call_upstream_json(
            llm=llm,
            flow=LLMFlow.LLM_GATEWAY,
            wire_id="7/m",
            upstream=_upstream(),
            input_messages=[],
            observe=anthropic_passthrough._observe_message,
        )
    assert result.status_code == 200
    llm._track_llm_cost.assert_called_once()
    assert span_recorder.flows == [LLMFlow.LLM_GATEWAY]
    assert span_recorder.spans[0].span_data.usage["total_tokens"] == 5


@pytest.mark.usefixtures("span_recorder")
def test_passthrough_hides_credential_rejections() -> None:
    with _patched_client(httpx.Response(401, json={"error": "bad key"})):
        with pytest.raises(OnyxError) as error:
            call_upstream_json(
                llm=fake_llm(),
                flow=LLMFlow.LLM_GATEWAY,
                wire_id="7/m",
                upstream=_upstream(),
                input_messages=[],
                observe=anthropic_passthrough._observe_message,
            )
    assert error.value.error_code is OnyxErrorCode.BAD_GATEWAY


@pytest.mark.usefixtures("span_recorder")
def test_passthrough_forwards_vendor_overload() -> None:
    with _patched_client(httpx.Response(529, json={"type": "error"})):
        result = call_upstream_json(
            llm=fake_llm(),
            flow=LLMFlow.LLM_GATEWAY,
            wire_id="7/m",
            upstream=_upstream(),
            input_messages=[],
            observe=anthropic_passthrough._observe_message,
        )
    assert result.status_code == 529
