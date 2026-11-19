from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from onyx.configs.constants import (
    TokenRateLimitAction,
    TokenRateLimitPeriod,
    TokenRateLimitScope,
)
from onyx.server.query_and_chat.token_limit import BudgetUnit

# One save holds at most this many rows: 3 periods x a generous number of models.
MAX_LIMITS_PER_CARD = 300


class SpendingLimitRequest(BaseModel):
    """One limit. Token budgets are in thousands of tokens, costs in cents."""

    period: TokenRateLimitPeriod
    model_configuration_id: int | None = None
    token_budget: int | None = Field(default=None, gt=0)
    cost_budget_cents: float | None = Field(default=None, gt=0)
    action: TokenRateLimitAction = TokenRateLimitAction.BLOCK
    threshold_percent: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        has_budget = self.token_budget is not None or self.cost_budget_cents is not None
        if self.threshold_percent is not None:
            if self.action != TokenRateLimitAction.ALERT_ONLY or has_budget:
                raise ValueError(
                    "A threshold is for an alert-only limit with no budget"
                )
        elif not has_budget:
            raise ValueError("Set a token budget, a cost budget or both")
        return self


class CardPutRequest(BaseModel):
    enabled: bool
    limits: list[SpendingLimitRequest] = Field(max_length=MAX_LIMITS_PER_CARD)


class SpendingLimitSnapshot(BaseModel):
    id: int
    period: TokenRateLimitPeriod
    model_configuration_id: int | None
    model_name: str | None
    token_budget: int | None
    cost_budget_cents: float | None
    action: TokenRateLimitAction
    threshold_percent: int | None
    # When the current period ends.
    reset_at: datetime
    # Pool cards only: raw tokens and cents used this period.
    used_tokens: float | None
    used_cost_cents: float | None


class SpendingLimitCard(BaseModel):
    enabled: bool
    limits: list[SpendingLimitSnapshot]


class GroupSpendingLimitCards(BaseModel):
    pool: SpendingLimitCard
    per_member: SpendingLimitCard


class GroupSpendingLimits(GroupSpendingLimitCards):
    user_group_id: int
    group_name: str
    member_count: int


class SpendingLimitsResponse(BaseModel):
    workspace_pool: SpendingLimitCard
    workspace_per_user: SpendingLimitCard
    groups: list[GroupSpendingLimits]


class EffectiveBudget(BaseModel):
    scope: TokenRateLimitScope
    period: TokenRateLimitPeriod
    unit: BudgetUnit
    # Raw tokens or cents.
    budget: float
    # The group whose allowance supplies it; None for workspace values.
    user_group_id: int | None


class EffectiveSpendingLimits(BaseModel):
    user_id: UUID
    budgets: list[EffectiveBudget]
