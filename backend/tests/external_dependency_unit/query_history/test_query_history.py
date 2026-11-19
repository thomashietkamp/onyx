"""Query history against the real Postgres and file store: the session list,
the detail view, anonymization, the disabled mode and the CSV export job."""

import csv
import io
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from onyx.agents.models import ExecutionStatus, InputSource
from onyx.background.task_utils import construct_query_history_report_name
from onyx.configs.constants import (
    DEFAULT_PERSONA_ID,
    FileOrigin,
    FileType,
    MessageType,
    QAFeedbackType,
    QueryHistoryType,
)
from onyx.db.chat import create_chat_session
from onyx.db.enums import IncognitoRecordMode
from onyx.db.models import (
    AcceptedChatInput,
    ChatMessage,
    ChatMessageFeedback,
    ChatResponseItem,
    ChatSession,
    User,
)
from onyx.db.tasks import delete_task_with_id, get_task_with_id
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.file_store.file_store import get_default_file_store
from onyx.llm.models import AssistantMessage, TextContent
from onyx.server.query_history import api
from onyx.server.query_history.export import (
    cleanup_query_history_exports,
    run_query_history_export,
)
from onyx.server.query_history.models import QueryHistoryExportStatus
from onyx.server.settings.models import Settings
from tests.external_dependency_unit.conftest import MakeTestUser


def _add_turn(
    db_session: Session,
    chat_session: ChatSession,
    parent: ChatMessage,
    question: str,
    answer: str,
    is_positive: bool | None,
    accepted: str | None = None,
) -> ChatMessage:
    user_message = ChatMessage(
        chat_session_id=chat_session.id,
        parent_message_id=parent.id,
        message=question,
        token_count=1,
        message_type=MessageType.USER,
    )
    db_session.add(user_message)
    db_session.flush()
    parent.latest_child_message_id = user_message.id
    ai_message = ChatMessage(
        chat_session_id=chat_session.id,
        parent_message_id=user_message.id,
        message=answer,
        token_count=1,
        message_type=MessageType.ASSISTANT,
    )
    db_session.add(ai_message)
    db_session.flush()
    user_message.latest_child_message_id = ai_message.id
    if accepted is not None:
        db_session.add(
            ChatResponseItem(
                id=f"item-{ai_message.id}",
                chat_message_id=ai_message.id,
                step_index=0,
                message=AssistantMessage(content=[TextContent(text=answer)]),
                inputs=[
                    AcceptedChatInput(
                        id=f"input-{ai_message.id}",
                        source=InputSource.HUMAN,
                        content=accepted,
                    )
                ],
                generation_status=ExecutionStatus.COMPLETE,
                is_answer=True,
            )
        )
    if is_positive is not None:
        db_session.add(
            ChatMessageFeedback(chat_message_id=ai_message.id, is_positive=is_positive)
        )
    db_session.flush()
    return ai_message


def _session(
    db_session: Session,
    owner: User,
    turns: list[tuple[str, str, bool | None]],
    mode: IncognitoRecordMode | None = None,
    accepted: str | None = None,
) -> ChatSession:
    chat_session = create_chat_session(
        db_session, "Chat", owner.id, DEFAULT_PERSONA_ID, incognito_record_mode=mode
    )
    parent = ChatMessage(
        chat_session_id=chat_session.id,
        message="",
        token_count=0,
        message_type=MessageType.SYSTEM,
    )
    db_session.add(parent)
    db_session.flush()
    for index, (question, answer, is_positive) in enumerate(turns):
        parent = _add_turn(
            db_session,
            chat_session,
            parent,
            question,
            answer,
            is_positive,
            accepted if index == 0 else None,
        )
    db_session.commit()
    return chat_session


@pytest.fixture
def owner(make_test_user: MakeTestUser) -> User:
    return make_test_user("query-history-owner")


@pytest.fixture
def admin(make_test_user: MakeTestUser) -> User:
    return make_test_user("query-history-admin", is_admin=True)


@pytest.fixture
def started_at() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=1)


@pytest.fixture
def request_ids() -> Generator[list[str], None, None]:
    """Removes the task rows and files of the exports that a test starts."""
    ids: list[str] = []
    yield ids
    from onyx.db.engine.sql_engine import get_session_with_current_tenant

    for request_id in ids:
        get_default_file_store().delete_file(
            construct_query_history_report_name(request_id), error_on_missing=False
        )
        with get_session_with_current_tenant() as db_session:
            delete_task_with_id(db_session, request_id)


def test_list_filters_and_anonymizes(
    db_session: Session,
    tenant_context: None,  # noqa: ARG001
    owner: User,
    admin: User,
    started_at: datetime,
) -> None:
    liked = _session(db_session, owner, [("Q1?", "A1", True), ("Q2?", "A2", None)])
    mixed = _session(db_session, owner, [("Q3?", "A3", True), ("Q4?", "A4", False)])
    plain = _session(db_session, owner, [("Q5?", "A5", None)])
    content_free = _session(
        db_session, owner, [("", "", None)], mode=IncognitoRecordMode.USAGE_ONLY
    )
    ours = {liked.id, mixed.id, plain.id, content_free.id}

    def page(
        feedback_type: QAFeedbackType | None,
        query_history_type: QueryHistoryType = QueryHistoryType.NORMAL,
    ) -> dict[UUID, api.ChatSessionMinimal]:
        result = api.get_chat_session_history(
            page_num=0,
            page_size=100,
            feedback_type=feedback_type,
            start_time=started_at,
            end_time=None,
            _=admin,
            query_history_type=query_history_type,
            db_session=db_session,
        )
        return {item.id: item for item in result.items if item.id in ours}

    every = page(None)
    assert set(every) == {liked.id, mixed.id, plain.id}
    assert every[liked.id].feedback_type == QAFeedbackType.LIKE
    assert every[mixed.id].feedback_type == QAFeedbackType.MIXED
    assert every[plain.id].feedback_type is None
    assert every[liked.id].first_user_message == "Q1?"
    assert every[liked.id].first_ai_message == "A1"
    assert every[liked.id].user_email == owner.email

    assert set(page(QAFeedbackType.LIKE)) == {liked.id}
    assert set(page(QAFeedbackType.MIXED)) == {mixed.id}
    assert set(page(QAFeedbackType.DISLIKE)) == set()

    anonymized = page(None, QueryHistoryType.ANONYMIZED)
    assert {item.user_email for item in anonymized.values()} == {None}

    detail = api.get_chat_session_admin(
        chat_session_id=mixed.id,
        _=admin,
        query_history_type=QueryHistoryType.ANONYMIZED,
        db_session=db_session,
    )
    assert detail.user_email is None
    assert [m.message for m in detail.messages] == ["Q3?", "A3", "Q4?", "A4"]
    assert [m.feedback_type for m in detail.messages] == [
        None,
        QAFeedbackType.LIKE,
        None,
        QAFeedbackType.DISLIKE,
    ]

    for_user = api.get_chat_sessions_for_user(
        user_id=owner.id,
        _=admin,
        query_history_type=QueryHistoryType.NORMAL,
        db_session=db_session,
    )
    assert {s.id for s in for_user.sessions} == {liked.id, mixed.id, plain.id}

    # Anonymized: a per-user list would tie sessions back to the user.
    with pytest.raises(OnyxError):
        api.get_chat_sessions_for_user(
            user_id=owner.id,
            _=admin,
            query_history_type=QueryHistoryType.ANONYMIZED,
            db_session=db_session,
        )

    with pytest.raises(OnyxError) as missing:
        api.get_chat_session_admin(
            chat_session_id=content_free.id,
            _=admin,
            query_history_type=QueryHistoryType.NORMAL,
            db_session=db_session,
        )
    assert missing.value.error_code == OnyxErrorCode.SESSION_NOT_FOUND


def test_disabled_refuses(tenant_context: None) -> None:  # noqa: ARG001
    with patch.object(
        api,
        "load_settings",
        return_value=Settings(query_history_type=QueryHistoryType.DISABLED),
    ):
        with pytest.raises(OnyxError) as refused:
            api._query_history_type()
    assert refused.value.error_code == OnyxErrorCode.FEATURE_DISABLED


def _start_export(
    db_session: Session, admin: User, start: datetime, request_ids: list[str]
) -> tuple[str, MagicMock]:
    with patch.object(api, "client_app") as client_app:
        response = api.start_query_history_export(
            start=start,
            end=None,
            _=admin,
            __=QueryHistoryType.NORMAL,
            db_session=db_session,
        )
    request_ids.append(response.request_id)
    return response.request_id, client_app


def _status(db_session: Session, admin: User, request_id: str) -> str:
    db_session.expire_all()
    return api.get_query_history_export_status(
        request_id=request_id,
        _=admin,
        __=QueryHistoryType.NORMAL,
        db_session=db_session,
    ).status


@pytest.mark.parametrize(
    "query_history_type", [QueryHistoryType.NORMAL, QueryHistoryType.ANONYMIZED]
)
def test_export_writes_csv(
    db_session: Session,
    tenant_context: None,  # noqa: ARG001
    owner: User,
    admin: User,
    started_at: datetime,
    request_ids: list[str],
    query_history_type: QueryHistoryType,
) -> None:
    chat_session = _session(
        db_session,
        owner,
        [("=HYPERLINK(1)", "A1", True), ("Q2?", "A2", None)],
        accepted="sent while answering",
    )
    request_id, client_app = _start_export(db_session, admin, started_at, request_ids)

    sent = client_app.send_task.call_args
    assert sent.kwargs["expires"] > 0
    assert sent.kwargs["kwargs"]["request_id"] == request_id
    assert _status(db_session, admin, request_id) == QueryHistoryExportStatus.PENDING

    with patch(
        "onyx.server.query_history.export.load_settings",
        return_value=Settings(query_history_type=query_history_type),
    ):
        run_query_history_export(
            request_id,
            datetime.fromisoformat(sent.kwargs["kwargs"]["start"]),
            datetime.now(UTC),
        )

    assert _status(db_session, admin, request_id) == QueryHistoryExportStatus.SUCCESS
    exports = api.list_query_history_exports(
        _=admin, __=QueryHistoryType.NORMAL, db_session=db_session
    )
    listed = next(e for e in exports if e.request_id == request_id)
    assert listed.status == QueryHistoryExportStatus.SUCCESS
    assert listed.start is not None and listed.end is not None

    content = (
        get_default_file_store()
        .read_file(construct_query_history_report_name(request_id), mode="b")
        .read()
        .decode("utf-8")
    )
    rows = [
        row
        for row in csv.DictReader(io.StringIO(content))
        if row["chat_session_id"] == str(chat_session.id)
    ]
    assert [row["ai_response"] for row in rows] == ["A1", "A2"]
    # Formula cells are neutralized, and accepted inputs follow the question.
    assert rows[0]["user_message"] == "'=HYPERLINK(1)\nsent while answering"
    assert rows[0]["feedback_type"] == QAFeedbackType.LIKE.value
    expected_email = (
        "" if query_history_type == QueryHistoryType.ANONYMIZED else owner.email
    )
    assert {row["user_email"] for row in rows} == {expected_email}


def test_export_send_failure_fails_job(
    db_session: Session,
    tenant_context: None,  # noqa: ARG001
    admin: User,
    started_at: datetime,
    request_ids: list[str],
) -> None:
    with patch.object(api, "client_app") as client_app:
        client_app.send_task.side_effect = ConnectionError("broker down")
        with pytest.raises(OnyxError) as refused:
            api.start_query_history_export(
                start=started_at,
                end=None,
                _=admin,
                __=QueryHistoryType.NORMAL,
                db_session=db_session,
            )
    assert refused.value.error_code == OnyxErrorCode.SERVICE_UNAVAILABLE
    exports = api.list_query_history_exports(
        _=admin, __=QueryHistoryType.NORMAL, db_session=db_session
    )
    failed = [e for e in exports if e.status == QueryHistoryExportStatus.FAILURE]
    assert failed
    request_ids.extend(e.request_id for e in failed)


def test_late_worker_deletes_its_file(
    db_session: Session,
    tenant_context: None,  # noqa: ARG001
    admin: User,
    started_at: datetime,
    request_ids: list[str],
) -> None:
    """A job that the cleanup failed while it ran must not publish its file."""
    request_id, _ = _start_export(db_session, admin, started_at, request_ids)

    def fail_during_run(*args: object, **kwargs: object) -> int:  # noqa: ARG001
        from onyx.db.engine.sql_engine import get_session_with_current_tenant
        from onyx.db.models import TaskStatus
        from onyx.db.tasks import fail_task_with_id

        with get_session_with_current_tenant() as other:
            fail_task_with_id(other, request_id, TaskStatus.STARTED)
        return 0

    with patch(
        "onyx.server.query_history.export.write_query_history_csv",
        side_effect=fail_during_run,
    ):
        run_query_history_export(request_id, started_at, datetime.now(UTC))

    assert _status(db_session, admin, request_id) == QueryHistoryExportStatus.FAILURE
    assert (
        get_default_file_store().has_file(
            construct_query_history_report_name(request_id),
            file_origin=FileOrigin.QUERY_HISTORY_CSV,
            file_type=FileType.CSV,
        )
        is False
    )
    # A recent failure stays listed after the cleanup.
    cleanup_query_history_exports()
    db_session.expire_all()
    assert get_task_with_id(db_session, request_id) is not None
