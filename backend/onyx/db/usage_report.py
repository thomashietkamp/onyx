"""Queries for the admin usage report: the report records and the rows of
its chat message and user CSVs. The usage rows come from `user_usage.py` and
`system_usage.py`."""

from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, selectinload

from onyx.configs.constants import MessageType
from onyx.db.enums import AccountType
from onyx.db.models import ChatMessage, ChatSession, Persona, UsageReport, User
from onyx.db.user_usage import DELETED_USER_EXPORT_EMAIL

# Accounts that are not people do not hold a seat.
_NON_SEAT_ACCOUNT_TYPES = (
    AccountType.BOT,
    AccountType.EXT_PERM_USER,
    AccountType.ANONYMOUS,
)


class UsageReportChatMessageRow(BaseModel):
    """One user message and one model reply to it."""

    chat_session_id: UUID
    user_id: UUID | None
    user_email: str
    time_sent: datetime
    agent_name: str | None
    message_tokens: int
    response_tokens: int | None
    model: str | None


class UsageReportUserRow(BaseModel):
    user_id: UUID
    email: str
    is_active: bool


def create_usage_report__no_commit(
    db_session: Session,
    report_name: str,
    requestor_user_id: UUID | None,
    period_from: datetime | None,
    period_to: datetime | None,
) -> UsageReport:
    report = UsageReport(
        report_name=report_name,
        requestor_user_id=requestor_user_id,
        period_from=period_from,
        period_to=period_to,
    )
    db_session.add(report)
    db_session.flush()
    return report


def get_usage_reports(db_session: Session) -> list[UsageReport]:
    """All reports, newest first, with their requestor loaded."""
    return list(
        db_session.scalars(
            select(UsageReport)
            .options(selectinload(UsageReport.requestor))
            .order_by(UsageReport.time_created.desc(), UsageReport.id.desc())
        )
    )


def get_usage_report_by_name(
    db_session: Session, report_name: str
) -> UsageReport | None:
    return db_session.scalar(
        select(UsageReport).where(UsageReport.report_name == report_name)
    )


def iter_usage_report_chat_message_rows(
    db_session: Session, start: datetime, end: datetime
) -> Iterator[UsageReportChatMessageRow]:
    """User messages of the chat sessions that started in [start, end), one row
    for each model reply. A message with no reply gets one row with no model."""
    reply = aliased(ChatMessage)
    query = (
        select(
            ChatMessage.chat_session_id,
            ChatSession.user_id,
            # Deleted users leave user_id NULL but keep their messages.
            func.coalesce(User.email, DELETED_USER_EXPORT_EMAIL),
            ChatMessage.time_sent,
            Persona.name,
            ChatMessage.token_count,
            reply.token_count,
            reply.model_display_name,
        )
        .join(ChatSession, ChatMessage.chat_session_id == ChatSession.id)
        # ChatSession.user_id on the left: User.id comes from the fastapi-users
        # base, which ty resolves as a plain value rather than a column.
        .outerjoin(User, ChatSession.user_id == User.id)
        .outerjoin(Persona, ChatSession.persona_id == Persona.id)
        .outerjoin(
            reply,
            (reply.parent_message_id == ChatMessage.id)
            & (reply.message_type == MessageType.ASSISTANT),
        )
        .where(
            ChatMessage.message_type == MessageType.USER,
            ChatSession.time_created >= start,
            ChatSession.time_created < end,
        )
        .order_by(ChatMessage.time_sent, ChatMessage.id, reply.id)
        .execution_options(stream_results=True)
    )
    for (
        chat_session_id,
        user_id,
        email,
        time_sent,
        agent_name,
        message_tokens,
        response_tokens,
        model,
    ) in db_session.execute(query).yield_per(1000):
        yield UsageReportChatMessageRow(
            chat_session_id=chat_session_id,
            user_id=user_id,
            user_email=str(email),
            time_sent=time_sent,
            agent_name=agent_name,
            message_tokens=message_tokens,
            response_tokens=response_tokens,
            model=model,
        )


def get_usage_report_user_rows(db_session: Session) -> list[UsageReportUserRow]:
    """The accounts of people, which hold seats."""
    # Table columns: the fastapi-users base hides the ORM column types.
    user_columns = User.__table__.c
    rows = db_session.execute(
        select(user_columns.id, user_columns.email, user_columns.is_active)
        .where(user_columns.account_type.not_in(_NON_SEAT_ACCOUNT_TYPES))
        .order_by(user_columns.email)
    ).all()
    return [
        UsageReportUserRow(user_id=user_id, email=email, is_active=is_active)
        for user_id, email, is_active in rows
    ]
