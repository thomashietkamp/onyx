"""Workspace analytics and per-agent stats.

The org-wide routes need full admin. The per-agent routes need
READ_AGENT_ANALYTICS at GATE 1 and ownership of the agent at GATE 2
(``can_view_persona_stats``); a global MANAGE_AGENTS holder reads every agent.
"""

import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.analytics import (
    fetch_active_user_counts_by_day,
    fetch_feedback_counts_by_day,
    fetch_onyxbot_counts_by_day,
    fetch_persona_message_counts_by_day,
    fetch_persona_unique_user_counts_by_day,
    fetch_persona_unique_user_total,
    fetch_query_counts_by_day,
    merge_daily_counts,
)
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.models import User
from onyx.db.persona import can_view_persona_stats, get_persona_by_id
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.analytics.models import (
    AgentDailyStats,
    AgentStatsResponse,
    OnyxbotAnalyticsResponse,
    PersonaMessageAnalyticsResponse,
    PersonaUniqueUsersResponse,
    QueryAnalyticsResponse,
    UserAnalyticsResponse,
)

router = APIRouter(prefix="/analytics")

_DEFAULT_RANGE = datetime.timedelta(days=30)


def _resolve_range(
    start: datetime.datetime | None, end: datetime.datetime | None
) -> tuple[datetime.datetime, datetime.datetime]:
    """Default to the 30 days that end now. A naive datetime is read as UTC."""
    resolved_end = end or datetime.datetime.now(datetime.UTC)
    resolved_start = start or resolved_end - _DEFAULT_RANGE
    if resolved_end.tzinfo is None:
        resolved_end = resolved_end.replace(tzinfo=datetime.UTC)
    if resolved_start.tzinfo is None:
        resolved_start = resolved_start.replace(tzinfo=datetime.UTC)
    if resolved_start > resolved_end:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT, "The start must not be after the end."
        )
    return resolved_start, resolved_end


def _assert_can_view_agent_stats(
    persona_id: int, user: User, db_session: Session
) -> None:
    """GATE 2: the caller must own the agent, or hold global MANAGE_AGENTS."""
    try:
        persona = get_persona_by_id(
            persona_id=persona_id,
            user=None,
            db_session=db_session,
            is_for_edit=False,
        )
    except ValueError:
        raise OnyxError(OnyxErrorCode.PERSONA_NOT_FOUND, "Agent not found.")
    if not can_view_persona_stats(user, persona, db_session):
        raise OnyxError(
            OnyxErrorCode.INSUFFICIENT_PERMISSIONS,
            "You can only view the stats of agents you own.",
        )


@router.get("/admin/query")
def get_query_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
    db_session: Session = Depends(get_session),
) -> list[QueryAnalyticsResponse]:
    start_dt, end_dt = _resolve_range(start, end)
    queries = fetch_query_counts_by_day(db_session, start_dt, end_dt)
    feedback = fetch_feedback_counts_by_day(db_session, start_dt, end_dt)
    likes = [(day, positive) for day, positive, _negative in feedback]
    dislikes = [(day, negative) for day, _positive, negative in feedback]
    return [
        QueryAnalyticsResponse(
            date=day,
            total_queries=counts[0],
            total_likes=counts[1],
            total_dislikes=counts[2],
        )
        for day, counts in merge_daily_counts(queries, likes, dislikes)
    ]


@router.get("/admin/user")
def get_user_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
    db_session: Session = Depends(get_session),
) -> list[UserAnalyticsResponse]:
    start_dt, end_dt = _resolve_range(start, end)
    return [
        UserAnalyticsResponse(date=day, total_active_users=count)
        for day, count in fetch_active_user_counts_by_day(db_session, start_dt, end_dt)
    ]


@router.get("/admin/onyxbot")
def get_onyxbot_analytics(
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
    db_session: Session = Depends(get_session),
) -> list[OnyxbotAnalyticsResponse]:
    start_dt, end_dt = _resolve_range(start, end)
    return [
        OnyxbotAnalyticsResponse(
            date=day, total_queries=total, auto_resolved=auto_resolved
        )
        for day, total, auto_resolved in fetch_onyxbot_counts_by_day(
            db_session, start_dt, end_dt
        )
    ]


@router.get("/admin/persona/messages")
def get_persona_messages(
    persona_id: int,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    user: User = Depends(require_permission(Permission.READ_AGENT_ANALYTICS)),
    db_session: Session = Depends(get_session),
) -> list[PersonaMessageAnalyticsResponse]:
    start_dt, end_dt = _resolve_range(start, end)
    _assert_can_view_agent_stats(persona_id, user, db_session)
    return [
        PersonaMessageAnalyticsResponse(
            date=day, total_messages=count, persona_id=persona_id
        )
        for day, count in fetch_persona_message_counts_by_day(
            db_session, persona_id, start_dt, end_dt
        )
    ]


@router.get("/admin/persona/unique-users")
def get_persona_unique_users(
    persona_id: int,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    user: User = Depends(require_permission(Permission.READ_AGENT_ANALYTICS)),
    db_session: Session = Depends(get_session),
) -> list[PersonaUniqueUsersResponse]:
    start_dt, end_dt = _resolve_range(start, end)
    _assert_can_view_agent_stats(persona_id, user, db_session)
    return [
        PersonaUniqueUsersResponse(date=day, unique_users=count, persona_id=persona_id)
        for day, count in fetch_persona_unique_user_counts_by_day(
            db_session, persona_id, start_dt, end_dt
        )
    ]


@router.get("/assistant/{assistant_id}/stats")
def get_assistant_stats(
    assistant_id: int,
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    user: User = Depends(require_permission(Permission.READ_AGENT_ANALYTICS)),
    db_session: Session = Depends(get_session),
) -> AgentStatsResponse:
    start_dt, end_dt = _resolve_range(start, end)
    _assert_can_view_agent_stats(assistant_id, user, db_session)
    messages = fetch_persona_message_counts_by_day(
        db_session, assistant_id, start_dt, end_dt
    )
    unique_users = fetch_persona_unique_user_counts_by_day(
        db_session, assistant_id, start_dt, end_dt
    )
    daily_stats = [
        AgentDailyStats(
            date=day, total_messages=counts[0], total_unique_users=counts[1]
        )
        for day, counts in merge_daily_counts(messages, unique_users)
    ]
    return AgentStatsResponse(
        daily_stats=daily_stats,
        total_messages=sum(entry.total_messages for entry in daily_stats),
        total_unique_users=fetch_persona_unique_user_total(
            db_session, assistant_id, start_dt, end_dt
        ),
    )
