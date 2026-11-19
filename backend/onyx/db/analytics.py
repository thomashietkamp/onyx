"""Daily usage counts for workspace analytics and per-agent stats.

All counts group by the UTC calendar day of ``ChatMessage.time_sent``.
"""

import datetime
from collections.abc import Sequence

from sqlalchemy import Date, and_, case, cast, exists, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from onyx.configs.constants import MessageType
from onyx.db.models import ChatMessage, ChatMessageFeedback, ChatSession


def _utc_day() -> ColumnElement[datetime.date]:
    return cast(func.timezone("UTC", ChatMessage.time_sent), Date)


def _in_range(start: datetime.datetime, end: datetime.datetime) -> ColumnElement[bool]:
    return and_(ChatMessage.time_sent >= start, ChatMessage.time_sent <= end)


def fetch_query_counts_by_day(
    db_session: Session, start: datetime.datetime, end: datetime.datetime
) -> list[tuple[datetime.date, int]]:
    """Count of user messages (queries) per day."""
    day = _utc_day()
    stmt = (
        select(day, func.count(ChatMessage.id))
        .where(ChatMessage.message_type == MessageType.USER, _in_range(start, end))
        .group_by(day)
        .order_by(day)
    )
    return [(row[0], int(row[1])) for row in db_session.execute(stmt).all()]


def fetch_feedback_counts_by_day(
    db_session: Session, start: datetime.datetime, end: datetime.datetime
) -> list[tuple[datetime.date, int, int]]:
    """Positive and negative feedback counts per day of the rated message."""
    day = _utc_day()
    stmt = (
        select(
            day,
            func.sum(case((ChatMessageFeedback.is_positive.is_(True), 1), else_=0)),
            func.sum(case((ChatMessageFeedback.is_positive.is_(False), 1), else_=0)),
        )
        .join(ChatMessage, ChatMessage.id == ChatMessageFeedback.chat_message_id)
        .where(_in_range(start, end))
        .group_by(day)
        .order_by(day)
    )
    return [
        (row[0], int(row[1] or 0), int(row[2] or 0))
        for row in db_session.execute(stmt).all()
    ]


def fetch_active_user_counts_by_day(
    db_session: Session, start: datetime.datetime, end: datetime.datetime
) -> list[tuple[datetime.date, int]]:
    """Count of distinct users that sent a message per day."""
    day = _utc_day()
    stmt = (
        select(day, func.count(func.distinct(ChatSession.user_id)))
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(
            ChatMessage.message_type == MessageType.USER,
            ChatSession.user_id.is_not(None),
            _in_range(start, end),
        )
        .group_by(day)
        .order_by(day)
    )
    return [(row[0], int(row[1])) for row in db_session.execute(stmt).all()]


def fetch_onyxbot_counts_by_day(
    db_session: Session, start: datetime.datetime, end: datetime.datetime
) -> list[tuple[datetime.date, int, int]]:
    """Slack bot answers per day, and how many of them got no follow-up request.

    An answer is auto-resolved when no feedback on it asks for follow-up help.
    """
    day = _utc_day()
    needs_followup = exists().where(
        ChatMessageFeedback.chat_message_id == ChatMessage.id,
        ChatMessageFeedback.required_followup.is_(True),
    )
    stmt = (
        select(
            day,
            func.count(ChatMessage.id),
            func.sum(case((needs_followup, 0), else_=1)),
        )
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(
            ChatSession.onyxbot_flow.is_(True),
            ChatMessage.message_type == MessageType.ASSISTANT,
            _in_range(start, end),
        )
        .group_by(day)
        .order_by(day)
    )
    return [
        (row[0], int(row[1]), int(row[2] or 0))
        for row in db_session.execute(stmt).all()
    ]


def fetch_persona_message_counts_by_day(
    db_session: Session,
    persona_id: int,
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[tuple[datetime.date, int]]:
    """Count of user messages sent to one agent per day."""
    day = _utc_day()
    stmt = (
        select(day, func.count(ChatMessage.id))
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(
            ChatSession.persona_id == persona_id,
            ChatMessage.message_type == MessageType.USER,
            _in_range(start, end),
        )
        .group_by(day)
        .order_by(day)
    )
    return [(row[0], int(row[1])) for row in db_session.execute(stmt).all()]


def fetch_persona_unique_user_counts_by_day(
    db_session: Session,
    persona_id: int,
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[tuple[datetime.date, int]]:
    """Count of distinct users that sent a message to one agent per day."""
    day = _utc_day()
    stmt = (
        select(day, func.count(func.distinct(ChatSession.user_id)))
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(
            ChatSession.persona_id == persona_id,
            ChatSession.user_id.is_not(None),
            ChatMessage.message_type == MessageType.USER,
            _in_range(start, end),
        )
        .group_by(day)
        .order_by(day)
    )
    return [(row[0], int(row[1])) for row in db_session.execute(stmt).all()]


def fetch_persona_unique_user_total(
    db_session: Session,
    persona_id: int,
    start: datetime.datetime,
    end: datetime.datetime,
) -> int:
    """Count of distinct users that sent a message to one agent in the range."""
    stmt = (
        select(func.count(func.distinct(ChatSession.user_id)))
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(
            ChatSession.persona_id == persona_id,
            ChatSession.user_id.is_not(None),
            ChatMessage.message_type == MessageType.USER,
            _in_range(start, end),
        )
    )
    return int(db_session.scalar(stmt) or 0)


def merge_daily_counts(
    *series: Sequence[tuple[datetime.date, int]],
) -> list[tuple[datetime.date, list[int]]]:
    """Join several (day, count) series on day, with 0 for a missing day."""
    days = sorted({day for entries in series for day, _ in entries})
    lookups = [dict(entries) for entries in series]
    return [(day, [lookup.get(day, 0) for lookup in lookups]) for day in days]
