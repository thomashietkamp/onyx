"""Reads of chat sessions for the admin query history: the paged list, one
session with its messages, and the batches that the CSV export walks.

Child sessions of subagents and content-free incognito sessions are not part of
the history. Soft-deleted sessions are, because retention owns hard deletes.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Row, Select, and_, false, func, or_, select, true
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.selectable import Subquery

from onyx.configs.constants import MessageType, QAFeedbackType
from onyx.db.chat import content_persisting_sessions_filter
from onyx.db.models import ChatMessage, ChatMessageFeedback, ChatSession

# Rows of a session list: the session and whether any of its answers got a
# like and a dislike.
SessionWithFeedback = tuple[ChatSession, bool, bool]


def feedback_type_from_flags(
    has_like: bool, has_dislike: bool
) -> QAFeedbackType | None:
    if has_like and has_dislike:
        return QAFeedbackType.MIXED
    if has_like:
        return QAFeedbackType.LIKE
    if has_dislike:
        return QAFeedbackType.DISLIKE
    return None


def _session_feedback() -> Subquery:
    return (
        select(
            ChatMessage.chat_session_id.label("chat_session_id"),
            func.bool_or(ChatMessageFeedback.is_positive.is_(True)).label("has_like"),
            func.bool_or(ChatMessageFeedback.is_positive.is_(False)).label(
                "has_dislike"
            ),
        )
        .join(
            ChatMessageFeedback, ChatMessageFeedback.chat_message_id == ChatMessage.id
        )
        .group_by(ChatMessage.chat_session_id)
        .subquery("session_feedback")
    )


def _history_sessions_filter() -> ColumnElement[bool]:
    return and_(
        ChatSession.spawned_by_message_id.is_(None),
        content_persisting_sessions_filter(),
    )


def _feedback_filter(
    feedback: Subquery, feedback_type: QAFeedbackType
) -> ColumnElement[bool]:
    has_like = func.coalesce(feedback.c.has_like, false())
    has_dislike = func.coalesce(feedback.c.has_dislike, false())
    if feedback_type == QAFeedbackType.LIKE:
        return and_(has_like == true(), has_dislike == false())
    if feedback_type == QAFeedbackType.DISLIKE:
        return and_(has_dislike == true(), has_like == false())
    return and_(has_like == true(), has_dislike == true())


def _sessions_select(
    feedback: Subquery,
    start: datetime | None,
    end: datetime | None,
    feedback_type: QAFeedbackType | None,
) -> Select[tuple[ChatSession, bool | None, bool | None]]:
    stmt = (
        select(ChatSession, feedback.c.has_like, feedback.c.has_dislike)
        .outerjoin(feedback, feedback.c.chat_session_id == ChatSession.id)
        .where(_history_sessions_filter())
    )
    if start is not None:
        stmt = stmt.where(ChatSession.time_created >= start)
    if end is not None:
        stmt = stmt.where(ChatSession.time_created <= end)
    if feedback_type is not None:
        stmt = stmt.where(_feedback_filter(feedback, feedback_type))
    return stmt


def _rows(
    rows: Sequence[Row[tuple[ChatSession, bool | None, bool | None]]],
) -> list[SessionWithFeedback]:
    return [(row[0], bool(row[1]), bool(row[2])) for row in rows]


def fetch_query_history_page(
    db_session: Session,
    *,
    start: datetime | None,
    end: datetime | None,
    feedback_type: QAFeedbackType | None,
    page_num: int,
    page_size: int,
) -> tuple[list[SessionWithFeedback], int]:
    """One page of sessions, newest first, and the count of all matches."""
    feedback = _session_feedback()
    stmt = _sessions_select(feedback, start, end, feedback_type)
    total = db_session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db_session.execute(
        stmt.options(selectinload(ChatSession.user), selectinload(ChatSession.persona))
        .order_by(ChatSession.time_created.desc(), ChatSession.id.desc())
        .offset(page_num * page_size)
        .limit(page_size)
    ).all()
    return _rows(rows), total


def fetch_query_history_sessions_for_user(
    db_session: Session, user_id: UUID, limit: int
) -> list[SessionWithFeedback]:
    feedback = _session_feedback()
    rows = db_session.execute(
        _sessions_select(feedback, None, None, None)
        .where(ChatSession.user_id == user_id)
        .options(selectinload(ChatSession.user), selectinload(ChatSession.persona))
        .order_by(ChatSession.time_created.desc(), ChatSession.id.desc())
        .limit(limit)
    ).all()
    return _rows(rows)


def fetch_query_history_sessions_after(
    db_session: Session,
    *,
    start: datetime,
    end: datetime,
    after: tuple[datetime, UUID] | None,
    limit: int,
) -> list[SessionWithFeedback]:
    """The next batch of sessions in the range, oldest first, after the
    `(time_created, id)` cursor."""
    feedback = _session_feedback()
    stmt = _sessions_select(feedback, start, end, None)
    if after is not None:
        after_time, after_id = after
        stmt = stmt.where(
            or_(
                ChatSession.time_created > after_time,
                and_(ChatSession.time_created == after_time, ChatSession.id > after_id),
            )
        )
    rows = db_session.execute(
        stmt.options(selectinload(ChatSession.user), selectinload(ChatSession.persona))
        .order_by(ChatSession.time_created, ChatSession.id)
        .limit(limit)
    ).all()
    return _rows(rows)


def fetch_query_history_session(
    db_session: Session, chat_session_id: UUID
) -> SessionWithFeedback | None:
    feedback = _session_feedback()
    row = db_session.execute(
        _sessions_select(feedback, None, None, None)
        .where(ChatSession.id == chat_session_id)
        .options(selectinload(ChatSession.user), selectinload(ChatSession.persona))
    ).first()
    if row is None:
        return None
    return row[0], bool(row[1]), bool(row[2])


def fetch_first_messages(
    db_session: Session, chat_session_ids: Sequence[UUID]
) -> dict[UUID, dict[MessageType, str]]:
    """The text of the first user message and the first answer of each
    session, by session and message type."""
    if not chat_session_ids:
        return {}
    rows = db_session.execute(
        select(
            ChatMessage.chat_session_id, ChatMessage.message_type, ChatMessage.message
        )
        .where(
            ChatMessage.chat_session_id.in_(chat_session_ids),
            ChatMessage.message_type.in_([MessageType.USER, MessageType.ASSISTANT]),
        )
        .distinct(ChatMessage.chat_session_id, ChatMessage.message_type)
        .order_by(ChatMessage.chat_session_id, ChatMessage.message_type, ChatMessage.id)
    ).all()
    first: dict[UUID, dict[MessageType, str]] = {}
    for chat_session_id, message_type, message in rows:
        first.setdefault(chat_session_id, {})[message_type] = message
    return first


def fetch_session_messages(
    db_session: Session, chat_session_ids: Sequence[UUID]
) -> dict[UUID, list[ChatMessage]]:
    """Every message of each session with its feedback, documents and
    response items, in id order."""
    if not chat_session_ids:
        return {}
    messages = db_session.scalars(
        select(ChatMessage)
        .where(ChatMessage.chat_session_id.in_(chat_session_ids))
        .options(
            selectinload(ChatMessage.chat_message_feedbacks),
            selectinload(ChatMessage.search_docs),
            selectinload(ChatMessage.response_items),
        )
        .order_by(ChatMessage.id)
    ).all()
    by_session: dict[UUID, list[ChatMessage]] = {}
    for message in messages:
        by_session.setdefault(message.chat_session_id, []).append(message)
    return by_session


def mainline_messages(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """The selected branch of a session, from the first message after the root.

    Follows `latest_child_message_id` from the root, as the chat page does.
    This reads only, unlike the chat history chain, which creates a missing
    root.
    """
    by_id = {message.id: message for message in messages}
    root = next((m for m in messages if m.parent_message_id is None), None)
    chain: list[ChatMessage] = []
    seen: set[int] = set()
    current = root
    while current is not None and current.latest_child_message_id is not None:
        child = by_id.get(current.latest_child_message_id)
        if child is None or child.id in seen:
            break
        seen.add(child.id)
        chain.append(child)
        current = child
    return chain
