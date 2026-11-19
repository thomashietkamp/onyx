"""Flow check, model resolution and the endpoint wiring."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from onyx.db.engine.sql_engine import get_session
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError, register_onyx_exception_handlers
from onyx.llm.model_response import Choice, Message, ModelResponse
from onyx.server.gateway import access, api
from onyx.server.gateway.access import (
    ResolvedGatewayModel,
    authorize_gateway_request,
    gateway_user,
    resolve_gateway_model,
    resolve_metered_gateway_model,
)
from onyx.server.manage.llm.models import LLMProviderView, ModelConfigurationView
from onyx.tracing.flows import LLMFlow
from tests.unit.onyx.server.gateway.conftest import USAGE, fake_llm, stream_chunk


def _provider(models: list[ModelConfigurationView]) -> LLMProviderView:
    return LLMProviderView(
        id=7, name="p", provider="openai", api_key="sk", model_configurations=models
    )


def _model(name: str, *, visible: bool = True, chat: bool = True) -> Any:
    return ModelConfigurationView(
        id=3,
        name=name,
        is_visible=visible,
        supports_chat=chat,
        supports_image_input=False,
    )


def _settings(enabled: bool) -> MagicMock:
    settings = MagicMock()
    settings.llm_gateway_enabled = enabled
    return settings


def test_request_without_token_scopes_is_rejected() -> None:
    with patch.object(access, "gateway_request_flow", return_value=None):
        with pytest.raises(OnyxError) as error:
            authorize_gateway_request(MagicMock(), MagicMock())
    assert error.value.error_code is OnyxErrorCode.INSUFFICIENT_PERMISSIONS


def test_workspace_switch_blocks_only_the_gateway_flow() -> None:
    with (
        patch.object(access, "load_settings", return_value=_settings(False)),
        patch.object(access, "gateway_request_flow", return_value=LLMFlow.LLM_GATEWAY),
    ):
        with pytest.raises(OnyxError) as error:
            authorize_gateway_request(MagicMock(), MagicMock())
    assert error.value.error_code is OnyxErrorCode.FEATURE_DISABLED

    with (
        patch.object(access, "load_settings", return_value=_settings(False)),
        patch.object(
            access, "gateway_request_flow", return_value=LLMFlow.CRAFT_LLM_GENERATION
        ),
    ):
        flow = authorize_gateway_request(MagicMock(), MagicMock())
    assert flow is LLMFlow.CRAFT_LLM_GENERATION


@pytest.mark.parametrize(
    "wire_id, provider",
    [
        ("gpt-x", _provider([_model("gpt-x")])),
        ("abc/gpt-x", _provider([_model("gpt-x")])),
        ("7/gpt-y", _provider([_model("gpt-x")])),
        ("7/gpt-x", _provider([_model("gpt-x", visible=False)])),
        ("7/gpt-x", _provider([_model("gpt-x", chat=False)])),
        ("7/gpt-x", None),
    ],
)
def test_unknown_model_is_404(wire_id: str, provider: LLMProviderView | None) -> None:
    with patch.object(
        access, "fetch_accessible_llm_provider_by_id", return_value=provider
    ):
        with pytest.raises(OnyxError) as error:
            resolve_gateway_model(MagicMock(), MagicMock(), wire_id)
    assert error.value.error_code is OnyxErrorCode.NOT_FOUND


def test_model_names_keep_their_slashes() -> None:
    provider = _provider([_model("meta/llama-4")])
    with patch.object(
        access, "fetch_accessible_llm_provider_by_id", return_value=provider
    ) as lookup:
        resolved = resolve_gateway_model(MagicMock(), MagicMock(), "7/meta/llama-4")
    assert resolved.model_name == "meta/llama-4"
    assert lookup.call_args.args[2] == 7


def test_metered_resolution_checks_every_limit() -> None:
    calls: list[str] = []
    user = MagicMock()
    with (
        patch.object(
            access,
            "check_token_rate_limits",
            side_effect=lambda _user: calls.append("all-models"),
        ),
        patch.object(
            access,
            "fetch_accessible_llm_provider_by_id",
            side_effect=lambda *_: (
                calls.append("resolve") or _provider([_model("gpt-x")])
            ),
        ),
        patch.object(
            access,
            "check_llm_cost_limit_for_provider",
            side_effect=lambda **_: calls.append("cost-cap"),
        ),
        patch.object(
            access,
            "check_model_spend_limits",
            side_effect=lambda _user, model_id: calls.append(f"model:{model_id}"),
        ),
    ):
        resolve_metered_gateway_model(MagicMock(), user, "7/gpt-x")
    assert calls == ["all-models", "resolve", "cost-cap", "model:3"]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    register_onyx_exception_handlers(app)
    app.include_router(api.router)
    app.dependency_overrides[gateway_user] = lambda: MagicMock(id="user-1")
    app.dependency_overrides[get_session] = lambda: MagicMock()
    resolved = ResolvedGatewayModel(
        wire_id="7/gpt-x", provider=_provider([_model("gpt-x")]), model=_model("gpt-x")
    )
    with (
        patch.object(
            api, "authorize_gateway_request", return_value=LLMFlow.LLM_GATEWAY
        ),
        patch.object(api, "resolve_metered_gateway_model", return_value=resolved),
        patch.object(api, "resolve_gateway_model", return_value=resolved),
        patch.object(api, "caller_identity_hash", return_value="hash"),
    ):
        yield TestClient(app)


@pytest.mark.usefixtures("span_recorder")
def test_chat_completions_endpoint_non_streaming(client: TestClient) -> None:
    llm = fake_llm()
    llm.invoke_raw.return_value = ModelResponse(
        id="r",
        created="1",
        choice=Choice(finish_reason="stop", message=Message(content="ok")),
        usage=USAGE,
    )
    with patch.object(api, "llm_from_provider", return_value=llm):
        response = client.post(
            "/gateway/v1/chat/completions",
            json={"model": "7/gpt-x", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "ok"


@pytest.mark.usefixtures("span_recorder")
def test_chat_completions_endpoint_streaming(client: TestClient) -> None:
    llm = fake_llm()
    llm.stream_raw.return_value = iter(
        [stream_chunk(content="ok", finish_reason="stop"), stream_chunk(usage=USAGE)]
    )
    with patch.object(api, "llm_from_provider", return_value=llm):
        response = client.post(
            "/gateway/v1/chat/completions",
            json={
                "model": "7/gpt-x",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.endswith("data: [DONE]\n\n")


def test_responses_refuses_previous_response_id(client: TestClient) -> None:
    response = client.post(
        "/gateway/v1/responses",
        json={"model": "7/gpt-x", "input": "hi", "previous_response_id": "resp_1"},
    )
    assert response.status_code == 501
    assert response.json()["error_code"] == "NOT_IMPLEMENTED"


def test_count_tokens_falls_back_to_an_estimate(client: TestClient) -> None:
    with (
        patch.object(api, "llm_from_provider", return_value=fake_llm()),
        patch.object(api, "estimate_input_tokens", return_value=12),
    ):
        response = client.post(
            "/gateway/v1/messages/count_tokens",
            json={"model": "7/gpt-x", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.json() == {"input_tokens": 12}
