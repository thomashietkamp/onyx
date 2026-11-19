"""The deployment's tier in this fork.

The fork has every paid feature in MIT code and no license, so it runs at the
highest tier. The tier checks in MIT code reach these functions by name
through ``onyx/utils/variable_functionality.py:fetch_ee_implementation_or_noop``.
"""

from onyx.db.enums import AccessType
from onyx.server.settings.models import Tier

FORK_TIER = Tier.ENTERPRISE


def get_tier() -> Tier:
    return FORK_TIER


def require_business_tier_for_multi_sso() -> None:
    return None


def require_business_tier_for_connector_group_restrictions() -> None:
    return None


def require_business_tier_for_personal_access(
    access_type: AccessType,  # noqa: ARG001
) -> None:
    return None


def require_business_tier_for_sync_access(
    access_type: AccessType,  # noqa: ARG001
) -> None:
    return None
