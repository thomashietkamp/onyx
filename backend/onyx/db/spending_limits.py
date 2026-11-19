"""Writes behind the spending limit admin page. A card is the set of limit rows
of one scope (and one group, for the group scopes). A save replaces every row
on the card in one transaction, under a card lock."""

from collections.abc import Sequence
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from onyx.configs.constants import (
    TokenRateLimitAction,
    TokenRateLimitPeriod,
    TokenRateLimitScope,
)
from onyx.db.models import (
    TokenRateLimit,
    TokenRateLimitUsageBaseline,
    User__UserGroup,
    UserGroup,
)
from onyx.db.user_usage import (
    get_usage_bucket_start,
    insert_user_usage_baselines__no_commit,
)
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError

GROUP_SCOPES = (TokenRateLimitScope.USER_GROUP, TokenRateLimitScope.USER_GROUP_MEMBER)
# Any fixed key; it only serializes saves of the two workspace cards.
_WORKSPACE_CARD_LOCK_KEY = 0x5BE4D11


class LimitSpec(BaseModel):
    """One row as a save sends it."""

    period: TokenRateLimitPeriod
    model_configuration_id: int | None
    token_budget: int | None
    cost_budget_cents: float | None
    action: TokenRateLimitAction
    threshold_percent: int | None

    def bucket(self) -> tuple[int | None, TokenRateLimitPeriod, str, int | None]:
        return (
            self.model_configuration_id,
            self.period,
            self.action.value,
            self.threshold_percent,
        )


def _row_bucket(
    row: TokenRateLimit,
) -> tuple[int | None, TokenRateLimitPeriod, str, int | None]:
    return (
        row.model_configuration_id,
        row.period,
        row.action.value,
        row.threshold_percent,
    )


def lock_card__no_commit(db_session: Session, user_group_id: int | None) -> None:
    """Serializes writes to one card. A group card locks its group row, so a
    missing group is NOT_FOUND."""
    if user_group_id is None:
        db_session.execute(select(func.pg_advisory_xact_lock(_WORKSPACE_CARD_LOCK_KEY)))
        return
    group_id = db_session.scalar(
        select(UserGroup.id).where(UserGroup.id == user_group_id).with_for_update()
    )
    if group_id is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "User group not found")


def fetch_card_limits(
    db_session: Session, scope: TokenRateLimitScope, user_group_id: int | None
) -> list[TokenRateLimit]:
    stmt = select(TokenRateLimit).where(TokenRateLimit.scope == scope)
    stmt = (
        stmt.where(TokenRateLimit.user_group_id.is_(None))
        if user_group_id is None
        else stmt.where(TokenRateLimit.user_group_id == user_group_id)
    )
    return list(db_session.scalars(stmt.order_by(TokenRateLimit.id)).all())


def fetch_limits_for_groups(
    db_session: Session, user_group_ids: Sequence[int]
) -> list[TokenRateLimit]:
    if not user_group_ids:
        return []
    return list(
        db_session.scalars(
            select(TokenRateLimit)
            .where(TokenRateLimit.user_group_id.in_(user_group_ids))
            .order_by(TokenRateLimit.id)
        ).all()
    )


def replace_card_limits__no_commit(
    db_session: Session,
    scope: TokenRateLimitScope,
    user_group_id: int | None,
    enabled: bool,
    specs: Sequence[LimitSpec],
) -> list[TokenRateLimit]:
    """Makes the card hold exactly ``specs``. A row that keeps its bucket is
    updated in place, so a current usage reset and its baselines stay. A
    disabled card keeps its rows, disabled."""
    lock_card__no_commit(db_session, user_group_id)
    existing = fetch_card_limits(db_session, scope, user_group_id)
    by_bucket = {_row_bucket(row): row for row in existing}
    kept: list[TokenRateLimit] = []
    for spec in specs:
        row = by_bucket.pop(spec.bucket(), None)
        if row is None:
            row = TokenRateLimit(
                scope=scope,
                user_group_id=user_group_id,
                period=spec.period,
                model_configuration_id=spec.model_configuration_id,
                action=spec.action,
                threshold_percent=spec.threshold_percent,
            )
            db_session.add(row)
        row.token_budget = spec.token_budget
        row.cost_budget_cents = spec.cost_budget_cents
        row.enabled = enabled
        kept.append(row)
    for row in by_bucket.values():
        db_session.delete(row)
    db_session.flush()
    return kept


def delete_group_limits__no_commit(db_session: Session, user_group_id: int) -> None:
    lock_card__no_commit(db_session, user_group_id)
    db_session.execute(
        delete(TokenRateLimit).where(TokenRateLimit.user_group_id == user_group_id)
    )


def fetch_token_rate_limit(db_session: Session, limit_id: int) -> TokenRateLimit | None:
    return db_session.get(TokenRateLimit, limit_id)


def reset_limit_usage__no_commit(db_session: Session, limit: TokenRateLimit) -> None:
    """From now on, the limit counts only new usage in its current period.
    Usage rows stay. The current bucket's usage is stored per user as the
    baseline that the limit subtracts."""
    lock_card__no_commit(db_session, limit.user_group_id)
    now = datetime.now(timezone.utc)
    db_session.execute(
        delete(TokenRateLimitUsageBaseline).where(
            TokenRateLimitUsageBaseline.token_rate_limit_id == limit.id
        )
    )
    limit.usage_reset_at = now
    insert_user_usage_baselines__no_commit(
        db_session, limit.id, get_usage_bucket_start(now), limit.model_configuration_id
    )
    db_session.flush()


def count_group_members(
    db_session: Session, user_group_ids: Sequence[int]
) -> dict[int, int]:
    if not user_group_ids:
        return {}
    rows = db_session.execute(
        select(User__UserGroup.user_group_id, func.count(User__UserGroup.user_id))
        .where(User__UserGroup.user_group_id.in_(user_group_ids))
        .group_by(User__UserGroup.user_group_id)
    ).all()
    return {group_id: int(count) for group_id, count in rows}
