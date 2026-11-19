"""Workspace analytics and agent stats against real Postgres.

The rows use dates in 1999, so the org-wide counts do not see other data.
"""

import datetime
from collections.abc import Generator
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from onyx.configs.constants import MessageType
from onyx.db.chat import create_chat_session
from onyx.db.enums import Permission
from onyx.db.models import ChatMessage, ChatMessageFeedback, ChatSession, Persona, User
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.analytics.api import (
    get_assistant_stats,
    get_onyxbot_analytics,
    get_persona_messages,
    get_persona_unique_users,
    get_query_analytics,
    get_user_analytics,
)
from tests.external_dependency_unit.conftest import MakeTestUser

DAY_1 = datetime.date(1999, 3, 1)
DAY_2 = datetime.date(1999, 3, 2)
START = datetime.datetime(1999, 3, 1, tzinfo=datetime.UTC)
END = datetime.datetime(1999, 3, 3, tzinfo=datetime.UTC)


def _at(day: datetime.date, hour: int) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, hour, tzinfo=datetime.UTC)


def _add_message(
    db_session: Session,
    chat_session: ChatSession,
    message_type: MessageType,
    time_sent: datetime.datetime,
) -> ChatMessage:
    message = ChatMessage(
        chat_session_id=chat_session.id,
        message="analytics test",
        token_count=1,
        message_type=message_type,
        time_sent=time_sent,
    )
    db_session.add(message)
    db_session.flush()
    return message


def _with_permissions(db_session: Session, user: User, *perms: Permission) -> User:
    user.effective_permissions = [perm.value for perm in perms]
    db_session.commit()
    db_session.refresh(user)
    return user


class _Seed:
    def __init__(self, owner: User, other: User, admin: User, persona: Persona) -> None:
        self.owner = owner
        self.other = other
        self.admin = admin
        self.persona = persona


@pytest.fixture
def seed(
    db_session: Session, tenant_context: None, make_test_user: MakeTestUser
) -> Generator[_Seed, None, None]:
    _ = tenant_context
    owner = make_test_user("analytics_owner")
    other = make_test_user("analytics_other")
    admin = make_test_user("analytics_admin", is_admin=True)
    persona = Persona(
        name=f"analytics-{uuid4().hex[:8]}",
        description="analytics test agent",
        user_id=owner.id,
        is_public=False,
    )
    db_session.add(persona)
    db_session.flush()

    owner_session = create_chat_session(
        db_session=db_session,
        description=None,
        user_id=owner.id,
        persona_id=persona.id,
    )
    other_session = create_chat_session(
        db_session=db_session,
        description=None,
        user_id=other.id,
        persona_id=persona.id,
    )
    bot_session = create_chat_session(
        db_session=db_session,
        description=None,
        user_id=other.id,
        persona_id=None,
        onyxbot_flow=True,
    )

    # Day 1: owner asks twice, other asks once; the owner likes one answer.
    _add_message(db_session, owner_session, MessageType.USER, _at(DAY_1, 9))
    liked = _add_message(
        db_session, owner_session, MessageType.ASSISTANT, _at(DAY_1, 9)
    )
    _add_message(db_session, owner_session, MessageType.USER, _at(DAY_1, 10))
    _add_message(db_session, other_session, MessageType.USER, _at(DAY_1, 23))
    # Day 2: owner asks once and dislikes the answer.
    _add_message(db_session, owner_session, MessageType.USER, _at(DAY_2, 1))
    disliked = _add_message(
        db_session, owner_session, MessageType.ASSISTANT, _at(DAY_2, 1)
    )
    # Day 2: two Slack bot answers, one asks for follow-up help.
    _add_message(db_session, bot_session, MessageType.USER, _at(DAY_2, 2))
    _add_message(db_session, bot_session, MessageType.ASSISTANT, _at(DAY_2, 2))
    _add_message(db_session, bot_session, MessageType.USER, _at(DAY_2, 3))
    followup = _add_message(
        db_session, bot_session, MessageType.ASSISTANT, _at(DAY_2, 3)
    )
    feedback = [
        ChatMessageFeedback(chat_message_id=liked.id, is_positive=True),
        ChatMessageFeedback(chat_message_id=disliked.id, is_positive=False),
        ChatMessageFeedback(chat_message_id=followup.id, required_followup=True),
    ]
    db_session.add_all(feedback)
    db_session.commit()

    try:
        yield _Seed(owner=owner, other=other, admin=admin, persona=persona)
    finally:
        db_session.rollback()
        db_session.execute(
            delete(ChatMessageFeedback).where(
                ChatMessageFeedback.id.in_([row.id for row in feedback])
            )
        )
        db_session.commit()


def test_query_analytics_counts_queries_and_feedback(
    seed: _Seed, db_session: Session
) -> None:
    result = get_query_analytics(
        start=START, end=END, _=seed.admin, db_session=db_session
    )
    by_day = {row.date: row for row in result}
    assert set(by_day) == {DAY_1, DAY_2}
    assert (by_day[DAY_1].total_queries, by_day[DAY_1].total_likes) == (3, 1)
    assert by_day[DAY_1].total_dislikes == 0
    # Day 2 holds the owner's query and the two Slack bot queries.
    assert (by_day[DAY_2].total_queries, by_day[DAY_2].total_dislikes) == (3, 1)


def test_user_analytics_counts_distinct_users(seed: _Seed, db_session: Session) -> None:
    result = get_user_analytics(
        start=START, end=END, _=seed.admin, db_session=db_session
    )
    assert {row.date: row.total_active_users for row in result} == {
        DAY_1: 2,
        DAY_2: 2,
    }


def test_onyxbot_analytics_counts_auto_resolved(
    seed: _Seed, db_session: Session
) -> None:
    result = get_onyxbot_analytics(
        start=START, end=END, _=seed.admin, db_session=db_session
    )
    assert [(row.date, row.total_queries, row.auto_resolved) for row in result] == [
        (DAY_2, 2, 1)
    ]


def test_owner_with_permission_reads_agent_stats(
    seed: _Seed, db_session: Session
) -> None:
    owner = _with_permissions(db_session, seed.owner, Permission.READ_AGENT_ANALYTICS)
    persona_id = seed.persona.id

    messages = get_persona_messages(
        persona_id=persona_id,
        start=START,
        end=END,
        user=owner,
        db_session=db_session,
    )
    assert [(row.date, row.total_messages) for row in messages] == [
        (DAY_1, 3),
        (DAY_2, 1),
    ]

    unique_users = get_persona_unique_users(
        persona_id=persona_id,
        start=START,
        end=END,
        user=owner,
        db_session=db_session,
    )
    assert [(row.date, row.unique_users) for row in unique_users] == [
        (DAY_1, 2),
        (DAY_2, 1),
    ]

    stats = get_assistant_stats(
        assistant_id=persona_id,
        start=START,
        end=END,
        user=owner,
        db_session=db_session,
    )
    assert stats.total_messages == 4
    # Distinct over the whole range, not the sum of the daily counts.
    assert stats.total_unique_users == 2
    assert [
        (row.date, row.total_messages, row.total_unique_users)
        for row in stats.daily_stats
    ] == [(DAY_1, 3, 2), (DAY_2, 1, 1)]


def test_non_owner_with_permission_is_refused(seed: _Seed, db_session: Session) -> None:
    other = _with_permissions(db_session, seed.other, Permission.READ_AGENT_ANALYTICS)
    with pytest.raises(OnyxError) as exc:
        get_assistant_stats(
            assistant_id=seed.persona.id,
            start=START,
            end=END,
            user=other,
            db_session=db_session,
        )
    assert exc.value.error_code == OnyxErrorCode.INSUFFICIENT_PERMISSIONS


def test_manage_agents_holder_reads_any_agent(seed: _Seed, db_session: Session) -> None:
    manager = _with_permissions(
        db_session,
        seed.other,
        Permission.MANAGE_AGENTS,
        Permission.READ_AGENT_ANALYTICS,
    )
    stats = get_assistant_stats(
        assistant_id=seed.persona.id,
        start=START,
        end=END,
        user=manager,
        db_session=db_session,
    )
    assert stats.total_messages == 4


def test_missing_agent_is_not_found(seed: _Seed, db_session: Session) -> None:
    with pytest.raises(OnyxError) as exc:
        get_assistant_stats(
            assistant_id=-1,
            start=START,
            end=END,
            user=seed.admin,
            db_session=db_session,
        )
    assert exc.value.error_code == OnyxErrorCode.PERSONA_NOT_FOUND


def test_start_after_end_is_invalid(seed: _Seed, db_session: Session) -> None:
    with pytest.raises(OnyxError) as exc:
        get_query_analytics(start=END, end=START, _=seed.admin, db_session=db_session)
    assert exc.value.error_code == OnyxErrorCode.INVALID_INPUT
