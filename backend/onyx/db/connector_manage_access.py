"""Manage roles of groups on cc-pairs (UserGroup__ConnectorCredentialPair).

Editors change and delete a pair and decide who manages it; Operators run it.
A group whose permission grants include global MANAGE_CONNECTORS (the Admin
group, or any group given that permission) is a fixed Editor of every pair: it
shows as a fixed row and holds no stored row.
"""

from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from onyx.db.document import mark_cc_pair_documents_for_sync__no_commit
from onyx.db.enums import ConnectorManageRole, Permission
from onyx.db.models import (
    PermissionGrant,
    User__UserGroup,
    UserGroup,
    UserGroup__ConnectorCredentialPair,
)

_FIXED_EDITOR_PERMISSIONS = (
    Permission.MANAGE_CONNECTORS,
    Permission.FULL_ADMIN_PANEL_ACCESS,
)


def fetch_manage_roles_for_cc_pair(
    db_session: Session, cc_pair_id: int
) -> dict[int, ConnectorManageRole]:
    """Stored (non-fixed) roles: group id -> role."""
    rows = db_session.execute(
        select(
            UserGroup__ConnectorCredentialPair.user_group_id,
            UserGroup__ConnectorCredentialPair.role,
        ).where(
            UserGroup__ConnectorCredentialPair.cc_pair_id == cc_pair_id,
            UserGroup__ConnectorCredentialPair.is_current.is_(True),
        )
    ).all()
    return {group_id: role for group_id, role in rows}  # noqa: C416


def fetch_fixed_editor_group_ids(db_session: Session) -> set[int]:
    return set(
        db_session.scalars(
            select(PermissionGrant.group_id).where(
                PermissionGrant.permission.in_(_FIXED_EDITOR_PERMISSIONS),
                PermissionGrant.is_deleted.is_(False),
            )
        ).all()
    )


def fetch_group_summaries(
    db_session: Session, group_ids: Collection[int]
) -> dict[int, tuple[str, int, int]]:
    """group id -> (name, members, managers)."""
    if not group_ids:
        return {}
    rows = db_session.execute(
        select(UserGroup.id, UserGroup.name).where(UserGroup.id.in_(group_ids))
    ).all()
    names: dict[int, str] = {group_id: name for group_id, name in rows}  # noqa: C416
    counts = {
        group_id: (int(members), int(managers or 0))
        for group_id, members, managers in db_session.execute(
            select(
                User__UserGroup.user_group_id,
                func.count(User__UserGroup.user_id),
                func.count(User__UserGroup.user_id).filter(
                    User__UserGroup.is_manager.is_(True)
                ),
            )
            .where(User__UserGroup.user_group_id.in_(group_ids))
            .group_by(User__UserGroup.user_group_id)
        ).all()
    }
    return {
        group_id: (name, *counts.get(group_id, (0, 0)))
        for group_id, name in names.items()
    }


def set_cc_pair_manage_roles__no_commit(
    db_session: Session,
    cc_pair_id: int,
    roles: dict[int, ConnectorManageRole],
) -> None:
    """Makes the pair's stored manage rows ``roles``. A change to which groups
    manage the pair rewrites the documents' ``group:`` entries on a PUBLIC pair,
    so the pair's documents are marked for sync."""
    rows = {
        row.user_group_id: row
        for row in db_session.scalars(
            select(UserGroup__ConnectorCredentialPair).where(
                UserGroup__ConnectorCredentialPair.cc_pair_id == cc_pair_id,
                UserGroup__ConnectorCredentialPair.is_current.is_(True),
            )
        ).all()
    }
    removed = set(rows) - set(roles)
    added = set(roles) - set(rows)
    for group_id in removed:
        db_session.delete(rows[group_id])
    for group_id, role in roles.items():
        if group_id in rows:
            rows[group_id].role = role
    db_session.flush()
    for group_id in added:
        db_session.add(
            UserGroup__ConnectorCredentialPair(
                user_group_id=group_id,
                cc_pair_id=cc_pair_id,
                role=roles[group_id],
                is_current=True,
            )
        )
    db_session.flush()
    if added or removed:
        mark_cc_pair_documents_for_sync__no_commit(db_session, [cc_pair_id])
