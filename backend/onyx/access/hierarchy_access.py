from sqlalchemy.orm import Session

from onyx.db.external_perm import fetch_external_group_ids_for_user
from onyx.db.models import User
from onyx.utils.variable_functionality import fetch_versioned_implementation


def _get_user_external_group_ids(db_session: Session, user: User) -> list[str]:
    """The user's synced external groups, in their stored (source-prefixed)
    form. Anonymous users have none."""
    if user.is_anonymous:
        return []
    return sorted(fetch_external_group_ids_for_user(db_session, user.id))


def get_user_external_group_ids(db_session: Session, user: User) -> list[str]:
    versioned_get_user_external_group_ids = fetch_versioned_implementation(
        "onyx.access.hierarchy_access", "_get_user_external_group_ids"
    )
    return versioned_get_user_external_group_ids(db_session, user)
