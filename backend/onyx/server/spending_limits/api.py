"""Admin API for spending limits, shaped like the admin page's cards: the
workspace pool, the workspace per-user default, and each group's pool and
per-member allowance.

A scoped group manager can read their group's cards and edit its pool. A
group's per-member card needs a full admin: a group allowance beats the
workspace default upward, so a manager could otherwise raise their own limit.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.auth.scoped_permissions import assert_manages_group
from onyx.configs.constants import TokenRateLimitScope
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.llm import fetch_model_configuration_by_id
from onyx.db.models import TokenRateLimit, User
from onyx.db.spending_limits import (
    LimitSpec,
    count_group_members,
    delete_group_limits__no_commit,
    fetch_card_limits,
    fetch_limits_for_groups,
    fetch_token_rate_limit,
    replace_card_limits__no_commit,
    reset_limit_usage__no_commit,
)
from onyx.db.token_limit import (
    commit_token_rate_limit,
    fetch_pool_usage_baselines,
    usage_baseline,
)
from onyx.db.user_group import fetch_user_group, fetch_user_groups
from onyx.db.user_usage import get_period_window_reset, get_pool_usage_buckets_since
from onyx.db.users import fetch_user_by_id
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.query_and_chat.token_limit import (
    BudgetUnit,
    BudgetWindow,
    budget_windows,
    fetch_member_allowance,
    invalidate_any_rate_limit_exists_cache,
    is_shared_spend_principal,
)
from onyx.server.spending_limits.models import (
    CardPutRequest,
    EffectiveBudget,
    EffectiveSpendingLimits,
    GroupSpendingLimitCards,
    GroupSpendingLimits,
    SpendingLimitCard,
    SpendingLimitSnapshot,
    SpendingLimitsResponse,
)

router = APIRouter(prefix="/admin/spending-limits")

_admin = require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)
_group_manager = require_permission(Permission.MANAGE_USER_GROUPS, allow_scope=True)

_POOL_SCOPES = (TokenRateLimitScope.GLOBAL, TokenRateLimitScope.USER_GROUP)


def _model_name(db_session: Session, model_configuration_id: int | None) -> str | None:
    if model_configuration_id is None:
        return None
    model = fetch_model_configuration_by_id(db_session, model_configuration_id)
    if model is None:
        return None
    return model.custom_display_name or model.display_name or model.name


def _snapshot(
    db_session: Session, limit: TokenRateLimit, now: datetime
) -> SpendingLimitSnapshot:
    used_tokens: float | None = None
    used_cost_cents: float | None = None
    if limit.scope in _POOL_SCOPES:
        baseline = usage_baseline(
            limit, fetch_pool_usage_baselines(db_session, [limit])
        )

        def window(unit: BudgetUnit, used_before_reset: float) -> BudgetWindow:
            return BudgetWindow(
                period=limit.period,
                unit=unit,
                budget=0,
                usage_reset_at=limit.usage_reset_at,
                usage_baseline=used_before_reset,
            )

        tokens = window(BudgetUnit.TOKENS, baseline.tokens)
        cost = window(BudgetUnit.COST, baseline.cost_cents)
        usage = get_pool_usage_buckets_since(
            db_session,
            tokens.usage_start(now),
            limit.model_configuration_id,
            limit.user_group_id,
        )
        used_tokens = tokens.used(usage.tokens, now)
        used_cost_cents = cost.used(usage.cost_cents, now)
    return SpendingLimitSnapshot(
        id=limit.id,
        period=limit.period,
        model_configuration_id=limit.model_configuration_id,
        model_name=_model_name(db_session, limit.model_configuration_id),
        token_budget=limit.token_budget,
        cost_budget_cents=limit.cost_budget_cents,
        action=limit.action,
        threshold_percent=limit.threshold_percent,
        reset_at=get_period_window_reset(now, limit.period),
        used_tokens=used_tokens,
        used_cost_cents=used_cost_cents,
    )


def _card(db_session: Session, limits: list[TokenRateLimit]) -> SpendingLimitCard:
    """A card with any enabled row reads as enabled and lists its enabled rows."""
    now = datetime.now(timezone.utc)
    enabled = [limit for limit in limits if limit.enabled]
    return SpendingLimitCard(
        enabled=bool(enabled),
        limits=[_snapshot(db_session, limit, now) for limit in enabled],
    )


def _scope_card(
    db_session: Session, scope: TokenRateLimitScope, user_group_id: int | None
) -> SpendingLimitCard:
    return _card(db_session, fetch_card_limits(db_session, scope, user_group_id))


def _group_cards(db_session: Session, user_group_id: int) -> GroupSpendingLimitCards:
    return GroupSpendingLimitCards(
        pool=_scope_card(db_session, TokenRateLimitScope.USER_GROUP, user_group_id),
        per_member=_scope_card(
            db_session, TokenRateLimitScope.USER_GROUP_MEMBER, user_group_id
        ),
    )


def _save(
    db_session: Session,
    scope: TokenRateLimitScope,
    user_group_id: int | None,
    request: CardPutRequest,
) -> SpendingLimitCard:
    specs = [
        LimitSpec(
            period=limit.period,
            model_configuration_id=limit.model_configuration_id,
            token_budget=limit.token_budget,
            cost_budget_cents=limit.cost_budget_cents,
            action=limit.action,
            threshold_percent=limit.threshold_percent,
        )
        for limit in request.limits
    ]
    for spec in specs:
        if (
            spec.model_configuration_id is not None
            and fetch_model_configuration_by_id(db_session, spec.model_configuration_id)
            is None
        ):
            raise OnyxError(OnyxErrorCode.INVALID_INPUT, "Unknown model")
    replace_card_limits__no_commit(
        db_session, scope, user_group_id, request.enabled, specs
    )
    commit_token_rate_limit(db_session)
    invalidate_any_rate_limit_exists_cache()
    return _scope_card(db_session, scope, user_group_id)


def _existing_group_id(db_session: Session, user_group_id: int) -> int:
    group = fetch_user_group(db_session, user_group_id)
    if group is None or group.is_up_for_deletion:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "User group not found")
    return group.id


@router.get("")
def get_spending_limits(
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> SpendingLimitsResponse:
    groups = [
        group
        for group in fetch_user_groups(db_session, only_up_to_date=False)
        if not group.is_up_for_deletion
    ]
    group_ids = [group.id for group in groups]
    members = count_group_members(db_session, group_ids)
    limits_by_group: dict[int, list[TokenRateLimit]] = {gid: [] for gid in group_ids}
    for limit in fetch_limits_for_groups(db_session, group_ids):
        if limit.user_group_id is not None:
            limits_by_group[limit.user_group_id].append(limit)
    return SpendingLimitsResponse(
        workspace_pool=_scope_card(db_session, TokenRateLimitScope.GLOBAL, None),
        workspace_per_user=_scope_card(db_session, TokenRateLimitScope.USER, None),
        groups=[
            GroupSpendingLimits(
                user_group_id=group.id,
                group_name=group.name,
                member_count=members.get(group.id, 0),
                pool=_card(
                    db_session,
                    [
                        limit
                        for limit in limits_by_group[group.id]
                        if limit.scope == TokenRateLimitScope.USER_GROUP
                    ],
                ),
                per_member=_card(
                    db_session,
                    [
                        limit
                        for limit in limits_by_group[group.id]
                        if limit.scope == TokenRateLimitScope.USER_GROUP_MEMBER
                    ],
                ),
            )
            for group in groups
        ],
    )


@router.put("/workspace/pool")
def put_workspace_pool(
    request: CardPutRequest,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> SpendingLimitCard:
    return _save(db_session, TokenRateLimitScope.GLOBAL, None, request)


@router.put("/workspace/per-user")
def put_workspace_per_user(
    request: CardPutRequest,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> SpendingLimitCard:
    return _save(db_session, TokenRateLimitScope.USER, None, request)


@router.get("/groups/{group_id}")
def get_group_spending_limits(
    group_id: int,
    user: User = Depends(_group_manager),
    db_session: Session = Depends(get_session),
) -> GroupSpendingLimitCards:
    assert_manages_group(user, db_session, group_id=group_id)
    return _group_cards(db_session, _existing_group_id(db_session, group_id))


@router.put("/groups/{group_id}/pool")
def put_group_pool(
    group_id: int,
    request: CardPutRequest,
    user: User = Depends(_group_manager),
    db_session: Session = Depends(get_session),
) -> SpendingLimitCard:
    assert_manages_group(user, db_session, group_id=group_id)
    group_id = _existing_group_id(db_session, group_id)
    return _save(db_session, TokenRateLimitScope.USER_GROUP, group_id, request)


@router.put("/groups/{group_id}/per-member")
def put_group_per_member(
    group_id: int,
    request: CardPutRequest,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> SpendingLimitCard:
    group_id = _existing_group_id(db_session, group_id)
    return _save(db_session, TokenRateLimitScope.USER_GROUP_MEMBER, group_id, request)


@router.delete("/groups/{group_id}")
def delete_group_spending_limits(
    group_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> None:
    delete_group_limits__no_commit(db_session, _existing_group_id(db_session, group_id))
    db_session.commit()
    invalidate_any_rate_limit_exists_cache()


def _reset(db_session: Session, limit: TokenRateLimit) -> SpendingLimitCard:
    reset_limit_usage__no_commit(db_session, limit)
    db_session.commit()
    return _scope_card(db_session, limit.scope, limit.user_group_id)


@router.post("/limits/{limit_id}/reset-usage")
def reset_limit_usage(
    limit_id: int,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> SpendingLimitCard:
    limit = fetch_token_rate_limit(db_session, limit_id)
    if limit is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "Spending limit not found")
    return _reset(db_session, limit)


@router.post("/groups/{group_id}/pool/limits/{limit_id}/reset-usage")
def reset_group_pool_limit_usage(
    group_id: int,
    limit_id: int,
    user: User = Depends(_group_manager),
    db_session: Session = Depends(get_session),
) -> SpendingLimitCard:
    assert_manages_group(user, db_session, group_id=group_id)
    limit = fetch_token_rate_limit(db_session, limit_id)
    if (
        limit is None
        or limit.user_group_id != group_id
        or limit.scope != TokenRateLimitScope.USER_GROUP
    ):
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "Spending limit not found")
    return _reset(db_session, limit)


@router.get("/users/{user_id}/effective")
def get_effective_spending_limits(
    user_id: UUID,
    _: User = Depends(_admin),
    db_session: Session = Depends(get_session),
) -> EffectiveSpendingLimits:
    """Each all-models budget the user is held to, and the group it comes from.
    A shared principal gets only the workspace pool."""
    target = fetch_user_by_id(db_session, user_id)
    if target is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "User not found")
    pool_limits = [
        limit
        for limit in fetch_card_limits(db_session, TokenRateLimitScope.GLOBAL, None)
        if limit.enabled and limit.model_configuration_id is None
    ]
    budgets = [
        EffectiveBudget(
            scope=TokenRateLimitScope.GLOBAL,
            period=window.period,
            unit=window.unit,
            budget=window.budget,
            user_group_id=None,
        )
        for window in budget_windows(pool_limits)
    ]
    if not is_shared_spend_principal(target):
        budgets.extend(
            EffectiveBudget(
                scope=(
                    TokenRateLimitScope.USER_GROUP_MEMBER
                    if window.user_group_id is not None
                    else TokenRateLimitScope.USER
                ),
                period=window.period,
                unit=window.unit,
                budget=window.budget,
                user_group_id=window.user_group_id,
            )
            for window in fetch_member_allowance(db_session, target.id, None)
        )
    return EffectiveSpendingLimits(user_id=target.id, budgets=budgets)
