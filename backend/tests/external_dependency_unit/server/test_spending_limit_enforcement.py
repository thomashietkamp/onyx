"""Group pools, group allowances and card saves, against a real database."""

from collections.abc import Generator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from onyx.configs.constants import (
    TokenRateLimitAction,
    TokenRateLimitPeriod,
    TokenRateLimitScope,
)
from onyx.db.enums import AccountType, UsageActorKind
from onyx.db.models import TokenRateLimit, User, User__UserGroup, UserGroup, UserUsage
from onyx.db.spending_limits import (
    LimitSpec,
    fetch_card_limits,
    replace_card_limits__no_commit,
    reset_limit_usage__no_commit,
)
from onyx.db.user_usage import get_usage_bucket_start
from onyx.error_handling.exceptions import OnyxError
from onyx.server.query_and_chat.token_limit import _check_spend_limits
from tests.external_dependency_unit.conftest import create_test_user


def _spec(cost_cents: float) -> LimitSpec:
    return LimitSpec(
        period=TokenRateLimitPeriod.DAILY,
        model_configuration_id=None,
        token_budget=None,
        cost_budget_cents=cost_cents,
        action=TokenRateLimitAction.BLOCK,
        threshold_percent=None,
    )


@pytest.fixture
def groups(db_session: Session) -> Generator[list[UserGroup], None, None]:
    made = [
        UserGroup(name=f"spend-{uuid4().hex[:8]}", is_up_to_date=True) for _ in "ab"
    ]
    db_session.add_all(made)
    db_session.commit()
    yield made
    db_session.execute(
        delete(TokenRateLimit).where(
            TokenRateLimit.user_group_id.in_([g.id for g in made])
        )
    )
    db_session.execute(
        delete(User__UserGroup).where(
            User__UserGroup.user_group_id.in_([g.id for g in made])
        )
    )
    db_session.execute(delete(UserGroup).where(UserGroup.id.in_([g.id for g in made])))
    db_session.commit()


def _member(db_session: Session, group: UserGroup, user: User) -> None:
    db_session.add(User__UserGroup(user_id=user.id, user_group_id=group.id))
    db_session.commit()


def _spend(db_session: Session, user: User, cost_cents: float) -> None:
    db_session.add(
        UserUsage(
            user_id=user.id,
            actor_kind=UsageActorKind.USER,
            window_start=get_usage_bucket_start(datetime.now(timezone.utc)),
            model="test-model",
            flow=f"test-{uuid4().hex[:8]}",
            input_tokens=1,
            output_tokens=1,
            cost_cents=cost_cents,
        )
    )
    db_session.commit()


def _save(
    db_session: Session,
    scope: TokenRateLimitScope,
    group: UserGroup,
    cost_cents: float,
) -> None:
    replace_card_limits__no_commit(
        db_session, scope, group.id, True, [_spec(cost_cents)]
    )
    db_session.commit()


def _is_blocked(user: User) -> str | None:
    try:
        _check_spend_limits(user, None)
    except OnyxError as e:
        return str(e.extra.get("scope") if e.extra else "")
    return None


@pytest.mark.usefixtures("tenant_context")
def test_group_pool_blocks_only_when_every_pool_is_used_up(
    db_session: Session, groups: list[UserGroup]
) -> None:
    small, large = groups
    user = create_test_user(db_session, "pool")
    _member(db_session, small, user)
    _member(db_session, large, user)
    _save(db_session, TokenRateLimitScope.USER_GROUP, small, 10)
    _save(db_session, TokenRateLimitScope.USER_GROUP, large, 50)

    _spend(db_session, user, 20)
    assert _is_blocked(user) is None

    _spend(db_session, user, 40)
    assert _is_blocked(user) == TokenRateLimitScope.USER_GROUP.value


@pytest.mark.usefixtures("tenant_context")
def test_largest_group_allowance_wins_and_service_accounts_skip_it(
    db_session: Session, groups: list[UserGroup]
) -> None:
    small, large = groups
    user = create_test_user(db_session, "allowance")
    service = create_test_user(
        db_session, "service", account_type=AccountType.SERVICE_ACCOUNT
    )
    for member in (user, service):
        _member(db_session, small, member)
        _member(db_session, large, member)
    _save(db_session, TokenRateLimitScope.USER_GROUP_MEMBER, small, 10)
    _save(db_session, TokenRateLimitScope.USER_GROUP_MEMBER, large, 30)

    _spend(db_session, user, 20)
    assert _is_blocked(user) is None
    _spend(db_session, user, 15)
    assert _is_blocked(user) == TokenRateLimitScope.USER_GROUP_MEMBER.value

    _spend(db_session, service, 100)
    assert _is_blocked(service) is None


@pytest.mark.usefixtures("tenant_context")
def test_card_save_keeps_a_reset_and_a_reset_frees_the_budget(
    db_session: Session, groups: list[UserGroup]
) -> None:
    group = groups[0]
    user = create_test_user(db_session, "reset")
    _member(db_session, group, user)
    _save(db_session, TokenRateLimitScope.USER_GROUP, group, 10)
    _spend(db_session, user, 15)
    assert _is_blocked(user) == TokenRateLimitScope.USER_GROUP.value

    (limit,) = fetch_card_limits(db_session, TokenRateLimitScope.USER_GROUP, group.id)
    reset_limit_usage__no_commit(db_session, limit)
    db_session.commit()
    assert _is_blocked(user) is None

    # A save with a new budget for the same bucket keeps the row and its reset.
    _save(db_session, TokenRateLimitScope.USER_GROUP, group, 12)
    (saved,) = fetch_card_limits(db_session, TokenRateLimitScope.USER_GROUP, group.id)
    assert saved.id == limit.id
    assert saved.usage_reset_at is not None
    assert saved.cost_budget_cents == 12
    assert _is_blocked(user) is None
