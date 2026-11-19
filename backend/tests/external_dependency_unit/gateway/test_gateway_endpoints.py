"""The gateway router against Postgres and a real OpenAI provider.

The PAT layer is replaced by a test middleware that sets the token scopes, and
the permission dependency returns the test user. The flow check, model
resolution, spending limits, translation, passthrough and tracing are real.
"""

from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from onyx.db.enums import Permission
from onyx.db.llm import (
    fetch_existing_llm_provider,
    remove_llm_provider,
    upsert_llm_provider,
)
from onyx.db.models import User
from onyx.error_handling.exceptions import register_onyx_exception_handlers
from onyx.llm.constants import LlmProviderNames
from onyx.server.gateway import api
from onyx.server.gateway.access import gateway_user
from onyx.server.manage.llm.models import (
    LLMProviderUpsertRequest,
    ModelConfigurationUpsertRequest,
)
from tests.common.models import CI_OPENAI_MODEL
from tests.external_dependency_unit.conftest import MakeTestUser
from tests.utils.secret_names import TestSecret


@contextmanager
def _openai_provider(db_session: Session, api_key: str) -> Generator[int, None, None]:
    name = f"gateway-test-{uuid4().hex}"
    provider = upsert_llm_provider(
        llm_provider_upsert_request=LLMProviderUpsertRequest(
            name=name,
            provider=LlmProviderNames.OPENAI,
            api_key=api_key,
            is_public=True,
            model_configurations=[
                ModelConfigurationUpsertRequest(name=CI_OPENAI_MODEL, is_visible=True)
            ],
            groups=[],
        ),
        db_session=db_session,
    )
    try:
        yield provider.id
    finally:
        db_session.rollback()
        existing = fetch_existing_llm_provider(name, db_session)
        if existing is not None:
            remove_llm_provider(db_session, existing.id)


@pytest.fixture
def catalog_provider_id(
    db_session: Session,
    tenant_context: None,  # noqa: ARG001
) -> Generator[int, None, None]:
    """A provider for the tests that make no provider call."""
    with _openai_provider(db_session, "sk-not-used") as provider_id:
        yield provider_id


@pytest.fixture
def live_provider_id(
    db_session: Session,
    tenant_context: None,  # noqa: ARG001
    test_secrets: dict[TestSecret, str],
) -> Generator[int, None, None]:
    with _openai_provider(
        db_session, test_secrets[TestSecret.OPENAI_API_KEY]
    ) as provider_id:
        yield provider_id


@pytest.fixture
def gateway_client(
    make_test_user: MakeTestUser,
    tenant_context: None,  # noqa: ARG001
) -> TestClient:
    user: User = make_test_user("gateway")
    app = FastAPI()
    register_onyx_exception_handlers(app)

    @app.middleware("http")
    async def scoped_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.token_scopes = [Permission.USE_LLM_GATEWAY]
        return await call_next(request)

    app.include_router(api.router)
    app.dependency_overrides[gateway_user] = lambda: user
    return TestClient(app)


def test_models_lists_the_wire_id(
    gateway_client: TestClient, catalog_provider_id: int
) -> None:
    response = gateway_client.get("/gateway/v1/models")
    assert response.status_code == 200, response.text
    ids = {entry["id"] for entry in response.json()["data"]}
    assert f"{catalog_provider_id}/{CI_OPENAI_MODEL}" in ids


def test_unknown_model_is_404(
    gateway_client: TestClient, catalog_provider_id: int
) -> None:
    response = gateway_client.post(
        "/gateway/v1/chat/completions",
        json={
            "model": f"{catalog_provider_id}/no-such-model",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"


@pytest.mark.secrets(TestSecret.OPENAI_API_KEY)
def test_chat_completion_streams_and_answers(
    gateway_client: TestClient, live_provider_id: int
) -> None:
    model = f"{live_provider_id}/{CI_OPENAI_MODEL}"
    messages = [{"role": "user", "content": "What is 2+2? Reply with the digit."}]

    plain = gateway_client.post(
        "/gateway/v1/chat/completions",
        json={"model": model, "messages": messages},
    )
    assert plain.status_code == 200, plain.text
    assert "4" in plain.json()["choices"][0]["message"]["content"]
    assert plain.json()["usage"]["total_tokens"] > 0

    streamed = gateway_client.post(
        "/gateway/v1/chat/completions",
        json={"model": model, "messages": messages, "stream": True},
    )
    assert streamed.status_code == 200
    assert streamed.text.endswith("data: [DONE]\n\n"), streamed.text[-500:]
    assert '"error"' not in streamed.text


@pytest.mark.secrets(TestSecret.OPENAI_API_KEY)
def test_responses_passthrough_is_stateless(
    gateway_client: TestClient, live_provider_id: int
) -> None:
    response = gateway_client.post(
        "/gateway/v1/responses",
        json={
            "model": f"{live_provider_id}/{CI_OPENAI_MODEL}",
            "input": "What is 2+2? Reply with the digit.",
            "store": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["store"] is False
    assert body["usage"]["total_tokens"] > 0
