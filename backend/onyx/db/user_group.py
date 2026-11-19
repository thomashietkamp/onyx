"""Invariants for the seeded default groups (Admin/Basic).

A default group holds members and nothing else: no connectors, document sets, agents,
skills, providers or limits, and its name is fixed. Its permission grants come from
migrations, except the few an admin may switch on the Basic group
(``DEFAULT_GROUP_TOGGLEABLE_PERMISSIONS``).

The rest of the module is this fork's group management: create, edit, rename, delete,
managers and permission grants, and the document-sync hooks that
``fetch_versioned_implementation("onyx.db.user_group", ...)`` resolves to.
"""

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, delete, func, select, union, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import KeyedColumnElement

from onyx.auth.permissions import (
    DEFAULT_GROUP_TOGGLEABLE_PERMISSIONS,
    NON_TOGGLEABLE_PERMISSIONS,
)
from onyx.db.enums import AccountType, ConnectorManageRole, GrantSource, Permission
from onyx.db.models import (
    ConnectorCredentialPair,
    Credential__UserGroup,
    DocumentByConnectorCredentialPair,
    DocumentSet__UserGroup,
    LLMProvider__UserGroup,
    MCPServer__UserGroup,
    PermissionGrant,
    Persona__UserGroup,
    User,
    User__UserGroup,
    UserGroup,
    UserGroup__CCPairDataAccess,
    UserGroup__ConnectorCredentialPair,
)
from onyx.db.permissions import (
    recompute_permissions_for_group__no_commit,
    recompute_user_permissions__no_commit,
)
from onyx.db.users import (
    DEFAULT_BASIC_GROUP_NAME,
    assert_admin_access_survives_removal,
    assert_group_membership_survives_removal,
)
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError


def group_permission_toggles_allowed(user_group: UserGroup) -> bool:
    """Custom groups take any toggleable grant, Basic its allowlist, Admin nothing."""
    return not user_group.is_default or user_group.name == DEFAULT_BASIC_GROUP_NAME


def assert_group_config_is_editable(
    db_session: Session, user_group_id: int, action: str
) -> None:
    """A default group holds only members, so the config writes this guards are refused.
    ``action`` completes "Cannot {action} a default system group.". A missing group falls
    through to the caller's own NOT_FOUND."""
    assert_groups_config_are_editable(db_session, [user_group_id], action)


def assert_groups_config_are_editable(
    db_session: Session, user_group_ids: Collection[int], action: str
) -> None:
    """Batch form for a caller holding several ids — one query rather than one per id."""
    if not user_group_ids:
        return

    is_default_present = db_session.scalar(
        select(UserGroup.id)
        .where(
            UserGroup.id.in_(user_group_ids),
            UserGroup.is_default.is_(True),
        )
        .limit(1)
    )
    if is_default_present is not None:
        raise OnyxError(
            OnyxErrorCode.CONFLICT, f"Cannot {action} a default system group."
        )


def assert_not_shared_with_default_group(
    db_session: Session, group_ids: Collection[int]
) -> None:
    """Keep a default group empty of everything but members, from any resource's own side.
    Basic holds the whole org and Admin every admin, so sharing with one is really "make
    this public", which every resource already expresses with its own public flag."""
    if not group_ids:
        return

    default_names = db_session.scalars(
        select(UserGroup.name).where(
            UserGroup.id.in_(group_ids),
            UserGroup.is_default.is_(True),
        )
    ).all()
    if default_names:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            "Cannot share with the default system group(s): "
            + ", ".join(sorted(default_names))
            + ". Make the resource public instead.",
        )


def assert_group_permissions_editable(
    user_group: UserGroup, permissions: Collection[Permission]
) -> None:
    """A custom group takes any toggleable grant. Basic takes only the allowlisted
    ones, since it holds the whole org, and Admin takes none."""
    if not user_group.is_default:
        return
    if not group_permission_toggles_allowed(user_group):
        raise OnyxError(
            OnyxErrorCode.CONFLICT,
            "Cannot change the permissions of a default system group.",
        )
    refused: list[str] = sorted(
        p.value for p in permissions if p not in DEFAULT_GROUP_TOGGLEABLE_PERMISSIONS
    )
    if refused:
        raise OnyxError(
            OnyxErrorCode.CONFLICT,
            f"Permissions {refused} cannot be toggled on the Basic group.",
        )


# ---------------------------------------------------------------------------
# Group management
# ---------------------------------------------------------------------------

# Link tables whose foreign key to user_group has no ON DELETE CASCADE. A hard
# delete removes their rows first, or it fails on the constraint.
_UNCASCADED_LINK_TABLES = (
    Persona__UserGroup,
    LLMProvider__UserGroup,
    DocumentSet__UserGroup,
    Credential__UserGroup,
    MCPServer__UserGroup,
)

# Accounts that take part in the group system (see AccountType).
_GROUP_ACCOUNT_TYPES = frozenset({AccountType.STANDARD, AccountType.SERVICE_ACCOUNT})


def fetch_user_groups(
    db_session: Session,
    only_up_to_date: bool = True,
    include_default: bool = True,
    group_ids: Collection[int] | None = None,
) -> list[UserGroup]:
    """Groups by name. The document sync calls it with ``only_up_to_date=False``."""
    stmt = select(UserGroup).order_by(UserGroup.name)
    if only_up_to_date:
        stmt = stmt.where(UserGroup.is_up_to_date.is_(True))
    if not include_default:
        stmt = stmt.where(UserGroup.is_default.is_(False))
    if group_ids is not None:
        stmt = stmt.where(UserGroup.id.in_(group_ids))
    return list(db_session.scalars(stmt).unique().all())


def fetch_user_group(db_session: Session, user_group_id: int) -> UserGroup | None:
    """Re-reads the row, so a sync loop never acts on a cached state."""
    return db_session.scalar(
        select(UserGroup)
        .where(UserGroup.id == user_group_id)
        .execution_options(populate_existing=True)
    )


def fetch_user_group_for_write(db_session: Session, user_group_id: int) -> UserGroup:
    """Locks the group row for a read-modify-write. A group up for deletion is
    gone as far as callers are concerned."""
    group = db_session.scalar(
        select(UserGroup)
        .where(UserGroup.id == user_group_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if group is None or group.is_up_for_deletion:
        raise OnyxError(
            OnyxErrorCode.NOT_FOUND, f"User group {user_group_id} not found."
        )
    return group


def assert_user_group_not_syncing(group: UserGroup) -> None:
    """Edits that change documents wait for the previous document sync, so two
    syncs never race on the same documents."""
    if not group.is_up_to_date:
        raise OnyxError(
            OnyxErrorCode.RESOURCE_SYNCING,
            f"User group '{group.name}' is still syncing. Try again in a moment.",
        )


def fetch_member_counts(
    db_session: Session, group_ids: Collection[int]
) -> dict[int, tuple[int, int]]:
    """group id -> (members, active members)."""
    if not group_ids:
        return {}
    is_active_col: KeyedColumnElement[Any] = User.__table__.c.is_active
    rows = db_session.execute(
        select(
            User__UserGroup.user_group_id,
            func.count(User__UserGroup.user_id),
            func.count(User__UserGroup.user_id).filter(is_active_col.is_(True)),
        )
        .join(User, User.id == User__UserGroup.user_id)  # ty: ignore[invalid-argument-type]
        .where(User__UserGroup.user_group_id.in_(group_ids))
        .group_by(User__UserGroup.user_group_id)
    ).all()
    counts = dict.fromkeys(group_ids, (0, 0))
    for group_id, members, active in rows:
        counts[group_id] = (int(members), int(active))
    return counts


def fetch_manager_ids(
    db_session: Session, group_ids: Collection[int]
) -> dict[int, list[UUID]]:
    managers: dict[int, list[UUID]] = {group_id: [] for group_id in group_ids}
    if not group_ids:
        return managers
    for group_id, user_id in db_session.execute(
        select(User__UserGroup.user_group_id, User__UserGroup.user_id).where(
            User__UserGroup.user_group_id.in_(group_ids),
            User__UserGroup.is_manager.is_(True),
            User__UserGroup.user_id.is_not(None),
        )
    ).all():
        managers[group_id].append(user_id)
    return managers


def fetch_member_ids(db_session: Session, group_id: int) -> set[UUID]:
    return {
        user_id
        for user_id in db_session.scalars(
            select(User__UserGroup.user_id).where(
                User__UserGroup.user_group_id == group_id,
                User__UserGroup.user_id.is_not(None),
            )
        ).all()
        if user_id is not None
    }


def _load_group_members(db_session: Session, user_ids: Collection[UUID]) -> list[User]:
    """Users that may join a group. Unknown ids are NOT_FOUND; accounts outside the
    group system (bots, external-permission users, anonymous) are INVALID_INPUT."""
    if not user_ids:
        return []
    users = list(
        db_session.scalars(
            select(User).where(User.id.in_(user_ids))  # ty: ignore[unresolved-attribute]
        )
        .unique()
        .all()
    )
    missing = set(user_ids) - {user.id for user in users}
    if missing:
        raise OnyxError(
            OnyxErrorCode.NOT_FOUND,
            f"User(s) not found: {', '.join(sorted(str(uid) for uid in missing))}",
        )
    refused = sorted(
        user.email for user in users if user.account_type not in _GROUP_ACCOUNT_TYPES
    )
    if refused:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            f"These accounts cannot join a group: {', '.join(refused)}",
        )
    return users


def load_cc_pairs(
    db_session: Session, cc_pair_ids: Collection[int]
) -> dict[int, ConnectorCredentialPair]:
    if not cc_pair_ids:
        return {}
    cc_pairs = {
        cc_pair.id: cc_pair
        for cc_pair in db_session.scalars(
            select(ConnectorCredentialPair).where(
                ConnectorCredentialPair.id.in_(cc_pair_ids)
            )
        ).all()
    }
    missing = set(cc_pair_ids) - set(cc_pairs)
    if missing:
        raise OnyxError(
            OnyxErrorCode.NOT_FOUND,
            f"Connector(s) not found: {', '.join(str(i) for i in sorted(missing))}",
        )
    return cc_pairs


def fetch_current_manage_roles(
    db_session: Session, group_id: int
) -> dict[int, ConnectorManageRole]:
    return {
        row.cc_pair_id: row.role
        for row in db_session.scalars(
            select(UserGroup__ConnectorCredentialPair).where(
                UserGroup__ConnectorCredentialPair.user_group_id == group_id,
                UserGroup__ConnectorCredentialPair.is_current.is_(True),
            )
        ).all()
    }


def fetch_current_manage_group_ids(
    db_session: Session, cc_pair_ids: Collection[int]
) -> dict[int, set[int]]:
    """cc-pair id -> the groups with a current manage row on it."""
    groups: dict[int, set[int]] = {cc_pair_id: set() for cc_pair_id in cc_pair_ids}
    if not cc_pair_ids:
        return groups
    for cc_pair_id, group_id in db_session.execute(
        select(
            UserGroup__ConnectorCredentialPair.cc_pair_id,
            UserGroup__ConnectorCredentialPair.user_group_id,
        ).where(
            UserGroup__ConnectorCredentialPair.cc_pair_id.in_(cc_pair_ids),
            UserGroup__ConnectorCredentialPair.is_current.is_(True),
        )
    ).all():
        groups[cc_pair_id].add(group_id)
    return groups


def _set_manage_rows__no_commit(
    db_session: Session,
    group: UserGroup,
    desired: dict[int, ConnectorManageRole],
) -> bool:
    """Makes the group's current manage rows equal ``desired``. A removed pair keeps
    an ``is_current=False`` row until the document sync has run, so the sync still
    finds its documents. Returns True when the set of pairs changed, which changes
    the documents' ACL."""
    rows = list(
        db_session.scalars(
            select(UserGroup__ConnectorCredentialPair).where(
                UserGroup__ConnectorCredentialPair.user_group_id == group.id
            )
        ).all()
    )
    current = {row.cc_pair_id: row for row in rows if row.is_current}
    stale_pair_ids = {row.cc_pair_id for row in rows if not row.is_current}

    removed = set(current) - set(desired)
    added = set(desired) - set(current)

    for cc_pair_id in removed:
        row = current[cc_pair_id]
        if cc_pair_id in stale_pair_ids:
            db_session.delete(row)
        else:
            row.is_current = False
    for cc_pair_id, role in desired.items():
        if cc_pair_id in current:
            current[cc_pair_id].role = role
    db_session.flush()
    for cc_pair_id in added:
        db_session.add(
            UserGroup__ConnectorCredentialPair(
                user_group_id=group.id,
                cc_pair_id=cc_pair_id,
                role=desired[cc_pair_id],
                is_current=True,
            )
        )
    db_session.flush()
    return bool(removed or added)


def _has_document_links(db_session: Session, group_id: int) -> bool:
    """Whether any document ACL can carry this group's name."""
    manage = db_session.scalar(
        select(UserGroup__ConnectorCredentialPair.cc_pair_id)
        .where(UserGroup__ConnectorCredentialPair.user_group_id == group_id)
        .limit(1)
    )
    if manage is not None:
        return True
    data_access = db_session.scalar(
        select(UserGroup__CCPairDataAccess.cc_pair_id)
        .where(UserGroup__CCPairDataAccess.user_group_id == group_id)
        .limit(1)
    )
    return data_access is not None


def assert_user_group_name_free(
    db_session: Session, name: str, exclude_group_id: int | None = None
) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, "A group needs a name.")
    stmt = select(UserGroup.id).where(func.lower(UserGroup.name) == cleaned.lower())
    if exclude_group_id is not None:
        stmt = stmt.where(UserGroup.id != exclude_group_id)
    if db_session.scalar(stmt.limit(1)) is not None:
        raise OnyxError(
            OnyxErrorCode.CONFLICT, f"A group named '{cleaned}' already exists."
        )
    return cleaned


def insert_user_group__no_commit(
    db_session: Session,
    name: str,
    user_ids: Collection[UUID],
    cc_pair_ids: Collection[int],
) -> UserGroup:
    """A new group gets the BASIC_ACCESS grant, like the seeded groups, and its
    cc-pairs as EDITOR manage rows."""
    cleaned = assert_user_group_name_free(db_session, name)
    members = _load_group_members(db_session, set(user_ids))
    cc_pairs = load_cc_pairs(db_session, set(cc_pair_ids))

    group = UserGroup(
        name=cleaned,
        # Only a group on cc-pairs changes document ACLs, so only then is a sync due.
        is_up_to_date=not cc_pairs,
        time_last_modified_by_user=func.now(),
    )
    db_session.add(group)
    db_session.flush()

    for user in members:
        db_session.add(User__UserGroup(user_group_id=group.id, user_id=user.id))
    db_session.add(
        PermissionGrant(
            group_id=group.id,
            permission=Permission.BASIC_ACCESS,
            grant_source=GrantSource.SYSTEM,
        )
    )
    db_session.flush()
    _set_manage_rows__no_commit(
        db_session,
        group,
        dict.fromkeys(cc_pairs, ConnectorManageRole.EDITOR),
    )
    recompute_user_permissions__no_commit([user.id for user in members], db_session)
    return group


def update_user_group_members__no_commit(
    db_session: Session,
    group: UserGroup,
    actor: User,
    user_ids: Collection[UUID],
) -> set[UUID]:
    """Replaces the members. Returns the ids whose membership changed. Removal keeps
    the workspace admin and every standard user in at least one group."""
    current = fetch_member_ids(db_session, group.id)
    desired = set(user_ids)
    removed = current - desired
    added = desired - current

    assert_admin_access_survives_removal(db_session, actor, group.id, list(removed))
    assert_group_membership_survives_removal(db_session, group.id, list(removed))
    _load_group_members(db_session, added)

    if removed:
        db_session.execute(
            delete(User__UserGroup).where(
                User__UserGroup.user_group_id == group.id,
                User__UserGroup.user_id.in_(removed),
            )
        )
    for user_id in added:
        db_session.add(User__UserGroup(user_group_id=group.id, user_id=user_id))
    db_session.flush()

    changed = removed | added
    recompute_user_permissions__no_commit(list(changed), db_session)
    return changed


def add_users_to_user_group__no_commit(
    db_session: Session, group: UserGroup, user_ids: Collection[UUID]
) -> None:
    added = set(user_ids) - fetch_member_ids(db_session, group.id)
    _load_group_members(db_session, added)
    for user_id in added:
        db_session.add(User__UserGroup(user_group_id=group.id, user_id=user_id))
    db_session.flush()
    recompute_user_permissions__no_commit(list(added), db_session)


def set_user_group_cc_pairs__no_commit(
    db_session: Session,
    group: UserGroup,
    roles: dict[int, ConnectorManageRole],
) -> None:
    """Replaces the group's manage rows. A change to the set of pairs marks the
    group for a document sync."""
    if group.is_default:
        if roles:
            raise OnyxError(
                OnyxErrorCode.CONFLICT,
                "Cannot attach connectors to a default system group.",
            )
        return
    load_cc_pairs(db_session, set(roles))
    if _set_manage_rows__no_commit(db_session, group, roles):
        group.is_up_to_date = False
        group.time_last_modified_by_user = func.now()


def rename_user_group__no_commit(
    db_session: Session, group: UserGroup, name: str
) -> None:
    """Document ACLs name the group, so a rename of a group on cc-pairs resyncs
    their documents."""
    cleaned = assert_user_group_name_free(db_session, name, exclude_group_id=group.id)
    if cleaned == group.name:
        return
    group.name = cleaned
    group.time_last_modified_by_user = func.now()
    if _has_document_links(db_session, group.id):
        group.is_up_to_date = False
    db_session.flush()


def set_user_group_manager__no_commit(
    db_session: Session, group: UserGroup, user_id: UUID, is_manager: bool
) -> None:
    edge = db_session.scalar(
        select(User__UserGroup).where(
            User__UserGroup.user_group_id == group.id,
            User__UserGroup.user_id == user_id,
        )
    )
    if edge is None:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            "Only a member of the group can be its manager. Add the user first.",
        )
    edge.is_manager = is_manager
    db_session.flush()
    recompute_user_permissions__no_commit(user_id, db_session)


def fetch_user_group_permissions(
    db_session: Session, group_id: int, include_non_toggleable: bool
) -> list[str]:
    granted = db_session.scalars(
        select(PermissionGrant.permission).where(
            PermissionGrant.group_id == group_id,
            PermissionGrant.is_deleted.is_(False),
        )
    ).all()
    return sorted(
        permission.value
        for permission in granted
        if include_non_toggleable or permission not in NON_TOGGLEABLE_PERMISSIONS
    )


def set_user_group_permissions__no_commit(
    db_session: Session, group: UserGroup, permission_values: Collection[str]
) -> list[str]:
    """Sets the group's toggleable grants. Non-toggleable grants (``basic``,
    ``admin``) come from the system and stay as they are."""
    try:
        desired = {Permission(value) for value in permission_values}
    except ValueError as e:
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, f"Unknown permission: {e}") from e

    assert_group_permissions_editable(group, desired)
    refused = sorted(p.value for p in desired if p in NON_TOGGLEABLE_PERMISSIONS)
    if refused:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            f"These permissions cannot be set on a group: {', '.join(refused)}",
        )

    grants = {
        grant.permission: grant
        for grant in db_session.scalars(
            select(PermissionGrant).where(PermissionGrant.group_id == group.id)
        ).all()
    }
    for permission, grant in grants.items():
        if permission in NON_TOGGLEABLE_PERMISSIONS:
            continue
        grant.is_deleted = permission not in desired
    for permission in desired - set(grants):
        db_session.add(
            PermissionGrant(
                group_id=group.id,
                permission=permission,
                grant_source=GrantSource.USER,
            )
        )
    db_session.flush()
    recompute_permissions_for_group__no_commit(group.id, db_session)
    return fetch_user_group_permissions(
        db_session, group.id, include_non_toggleable=False
    )


def delete_user_group__no_commit(db_session: Session, group: UserGroup) -> None:
    """Removes the group and every row that points at it."""
    for table in _UNCASCADED_LINK_TABLES:
        db_session.execute(delete(table).where(table.user_group_id == group.id))
    db_session.execute(
        delete(UserGroup__ConnectorCredentialPair).where(
            UserGroup__ConnectorCredentialPair.user_group_id == group.id
        )
    )
    db_session.execute(
        delete(User__UserGroup).where(User__UserGroup.user_group_id == group.id)
    )
    db_session.delete(group)
    db_session.flush()


def request_user_group_deletion__no_commit(
    db_session: Session, group: UserGroup, actor: User
) -> None:
    """Members lose the group's permissions at once. A group whose name can be in
    document ACLs stays, marked for deletion, until the document sync has removed
    it from them; then the sync deletes the row. Keeping the row also keeps the
    name taken, so a new group cannot inherit stale ACL entries."""
    member_ids = list(fetch_member_ids(db_session, group.id))
    assert_admin_access_survives_removal(db_session, actor, group.id, member_ids)
    assert_group_membership_survives_removal(db_session, group.id, member_ids)

    if not _has_document_links(db_session, group.id):
        delete_user_group__no_commit(db_session, group)
        recompute_user_permissions__no_commit(member_ids, db_session)
        return

    db_session.execute(
        delete(User__UserGroup).where(User__UserGroup.user_group_id == group.id)
    )
    for grant in group.permission_grants:
        grant.is_deleted = True
    db_session.execute(
        update(UserGroup__ConnectorCredentialPair)
        .where(UserGroup__ConnectorCredentialPair.user_group_id == group.id)
        .values(is_current=False)
    )
    group.is_up_for_deletion = True
    group.is_up_to_date = False
    db_session.flush()
    recompute_user_permissions__no_commit(member_ids, db_session)


def mark_user_group_as_synced__no_commit(db_session: Session, group: UserGroup) -> None:
    db_session.execute(
        delete(UserGroup__ConnectorCredentialPair).where(
            UserGroup__ConnectorCredentialPair.user_group_id == group.id,
            UserGroup__ConnectorCredentialPair.is_current.is_(False),
        )
    )
    group.is_up_to_date = True
    db_session.flush()


def construct_document_id_select_by_usergroup(user_group_id: int) -> Select:
    """Documents whose ACL may change with this group: those on any cc-pair the
    group manages now or did before the last edit, or gives data access to."""
    linked_cc_pair_ids = union(
        select(UserGroup__ConnectorCredentialPair.cc_pair_id).where(
            UserGroup__ConnectorCredentialPair.user_group_id == user_group_id
        ),
        select(UserGroup__CCPairDataAccess.cc_pair_id).where(
            UserGroup__CCPairDataAccess.user_group_id == user_group_id
        ),
    ).subquery()
    return (
        select(DocumentByConnectorCredentialPair.id)
        .join(
            ConnectorCredentialPair,
            and_(
                ConnectorCredentialPair.connector_id
                == DocumentByConnectorCredentialPair.connector_id,
                ConnectorCredentialPair.credential_id
                == DocumentByConnectorCredentialPair.credential_id,
            ),
        )
        .where(ConnectorCredentialPair.id.in_(select(linked_cc_pair_ids.c.cc_pair_id)))
        .distinct()
        .order_by(DocumentByConnectorCredentialPair.id)
    )


def delete_user_group_cc_pair_relationship__no_commit(
    cc_pair_id: int, db_session: Session
) -> None:
    """Called when a connector is deleted. Data-access rows go with the pair."""
    db_session.execute(
        delete(UserGroup__ConnectorCredentialPair).where(
            UserGroup__ConnectorCredentialPair.cc_pair_id == cc_pair_id
        )
    )
