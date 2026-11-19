"""The hook executor's fail strategies and failure log, with the HTTP call faked."""

from collections.abc import Generator
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from onyx.db.enums import HookFailStrategy, HookPoint
from onyx.db.hook import create_hook__no_commit
from onyx.db.models import Hook, HookExecutionLog
from onyx.error_handling.exceptions import OnyxError
from onyx.hooks import executor
from onyx.hooks.executor import HookSkipped, HookSoftFailed, execute_hook
from onyx.utils.external_endpoint import ExternalEndpointOutcome


class _Reply(BaseModel):
    query: str


def _outcome(success: bool, reachable: bool | None) -> ExternalEndpointOutcome:
    return ExternalEndpointOutcome(
        is_success=success,
        reachability_signal=reachable,
        status_code=200 if success else None,
        error_message=None if success else "unreachable",
        response_payload={"query": "rewritten"} if success else None,
        duration_ms=5,
    )


@pytest.fixture
def make_hook(db_session: Session) -> Generator[type[Hook], None, None]:
    db_session.execute(
        delete(Hook).where(Hook.hook_point == HookPoint.QUERY_PROCESSING)
    )
    db_session.commit()
    yield Hook
    db_session.execute(
        delete(Hook).where(Hook.hook_point == HookPoint.QUERY_PROCESSING)
    )
    db_session.commit()


def _create(db_session: Session, strategy: HookFailStrategy, active: bool) -> Hook:
    hook = create_hook__no_commit(
        db_session=db_session,
        name="test hook",
        hook_point=HookPoint.QUERY_PROCESSING,
        endpoint_url="https://hooks.example.com",
        fail_strategy=strategy,
        timeout_seconds=5,
        is_active=active,
        is_reachable=True,
    )
    db_session.commit()
    return hook


def _run(db_session: Session) -> _Reply | HookSkipped | HookSoftFailed:
    return execute_hook(
        db_session=db_session,
        hook_point=HookPoint.QUERY_PROCESSING,
        payload={"query": "hi"},
        response_type=_Reply,
    )


@pytest.mark.usefixtures("tenant_context", "make_hook")
def test_inactive_hook_is_skipped(db_session: Session) -> None:
    _create(db_session, HookFailStrategy.HARD, active=False)
    with patch.object(executor, "post_json_to_endpoint") as post:
        assert isinstance(_run(db_session), HookSkipped)
    post.assert_not_called()


@pytest.mark.usefixtures("tenant_context", "make_hook")
def test_success_returns_the_validated_reply(db_session: Session) -> None:
    _create(db_session, HookFailStrategy.HARD, active=True)
    with patch.object(
        executor,
        "post_json_to_endpoint",
        return_value=(_outcome(True, True), _Reply(query="rewritten")),
    ):
        result = _run(db_session)
    assert isinstance(result, _Reply) and result.query == "rewritten"


@pytest.mark.usefixtures("tenant_context", "make_hook")
def test_soft_failure_continues_and_hard_failure_raises(db_session: Session) -> None:
    hook = _create(db_session, HookFailStrategy.SOFT, active=True)
    with patch.object(
        executor, "post_json_to_endpoint", return_value=(_outcome(False, False), None)
    ):
        assert isinstance(_run(db_session), HookSoftFailed)

        hook.fail_strategy = HookFailStrategy.HARD
        db_session.commit()
        with pytest.raises(OnyxError):
            _run(db_session)

    db_session.expire_all()
    logs = db_session.scalars(
        select(HookExecutionLog).where(HookExecutionLog.hook_id == hook.id)
    ).all()
    assert len(logs) == 2
    assert all(not log.is_success for log in logs)
    refreshed = db_session.get(Hook, hook.id)
    assert refreshed is not None and refreshed.is_reachable is False
