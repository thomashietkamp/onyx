"""User group routes.

Authorization has two gates. GATE 1 (``require_permission``) lets a caller reach
the handler; ``allow_scope=True`` also admits a scoped group manager. GATE 2 in the
handler then limits a scoped manager to the groups they manage
(``assert_manages_group``) and to the cc-pairs within that scope
(``_scope_manage_roles``).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from onyx.auth.permission_projection import user_group_permissions
from onyx.auth.permissions import (
    PERMISSION_REGISTRY,
    PermissionAuthority,
    PermissionRegistryEntry,
    has_global_permission,
    has_permission,
    require_permission,
)
from onyx.auth.scoped_permissions import (
    assert_global,
    assert_manages_group,
    get_visible_user_group_ids,
    manages_group,
)
from onyx.db.cc_pair_data_access import (
    assert_restricted_cc_pairs_keep_a_group,
    fetch_data_access_cc_pair_ids_for_group,
    set_cc_pair_data_access_groups__no_commit,
)
from onyx.db.connector_credential_pair import (
    CCPairAccessLevel,
    get_connector_credential_pairs_for_user,
)
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import ConnectorManageRole, Permission
from onyx.db.models import User
from onyx.db.persona import fetch_persona_owner_ids, mark_persona_user_files_for_sync
from onyx.db.persona_sharing import get_user_group_ids_for_user
from onyx.db.scoped_permissions import fetch_managed_group_ids
from onyx.db.user_group import (
    assert_group_config_is_editable,
    fetch_user_group,
    fetch_user_groups,
    group_permission_toggles_allowed,
)
from onyx.db.user_group_management import (
    add_users_to_user_group__no_commit,
    assert_user_group_not_syncing,
    fetch_current_manage_roles,
    fetch_manager_ids,
    fetch_member_counts,
    fetch_user_group_for_write,
    fetch_user_group_permissions,
    insert_user_group__no_commit,
    load_cc_pairs,
    rename_user_group__no_commit,
    request_user_group_deletion__no_commit,
    set_user_group_agents__no_commit,
    set_user_group_cc_pairs__no_commit,
    set_user_group_document_sets__no_commit,
    set_user_group_manager__no_commit,
    set_user_group_permissions__no_commit,
    update_user_group_members__no_commit,
)
from onyx.db.users import fetch_user_by_id
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.user_group.models import (
    AddUsersRequest,
    DataAccessCCPairs,
    GroupDocumentSetChange,
    GroupLinkChange,
    ManagedCCPair,
    MinimalUserGroupSnapshot,
    SetDataAccessCCPairsRequest,
    SetIncognitoRequest,
    SetManagedCCPairsRequest,
    SetManagerRequest,
    SetPermissionsRequest,
    UserGroupCreate,
    UserGroupRename,
    UserGroupSnapshot,
    UserGroupUpdate,
)

router = APIRouter(prefix="/manage")

_read_groups = require_permission(Permission.READ_USER_GROUPS, allow_scope=True)
_manage_groups_scoped = require_permission(
    Permission.MANAGE_USER_GROUPS, allow_scope=True
)
_manage_groups_global = require_permission(Permission.MANAGE_USER_GROUPS)
_full_admin = require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)


def _snapshots(
    db_session: Session,
    user: User,
    group_ids: list[int] | None,
    *,
    include_default: bool,
    include_users: bool,
) -> list[UserGroupSnapshot]:
    groups = fetch_user_groups(
        db_session,
        only_up_to_date=False,
        include_default=include_default,
        group_ids=group_ids,
    )
    ids = [group.id for group in groups]
    counts = fetch_member_counts(db_session, ids)
    managers = fetch_manager_ids(db_session, ids)
    managed = fetch_managed_group_ids(user, db_session)
    is_user_groups_admin = has_global_permission(user, Permission.MANAGE_USER_GROUPS)
    is_full_admin = has_global_permission(user, Permission.FULL_ADMIN_PANEL_ACCESS)
    return [
        UserGroupSnapshot.from_model(
            group,
            include_users=include_users,
            member_count=counts[group.id][0],
            active_member_count=counts[group.id][1],
            manager_ids=managers[group.id],
            permissions=user_group_permissions(
                can_manage=manages_group(
                    user, db_session, group_id=group.id, managed_group_ids=managed
                ),
                is_user_groups_admin=is_user_groups_admin,
                is_full_admin=is_full_admin,
                is_default=group.is_default,
                permission_toggles_allowed=group_permission_toggles_allowed(group),
            ),
        )
        for group in groups
    ]


def _snapshot(db_session: Session, user: User, group_id: int) -> UserGroupSnapshot:
    snapshots = _snapshots(
        db_session, user, [group_id], include_default=True, include_users=True
    )
    if not snapshots:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, f"User group {group_id} not found.")
    return snapshots[0]


def _assert_may_edit_members(user: User, db_session: Session, group_id: int) -> None:
    """A default group takes membership edits from full admins only."""
    group = fetch_user_group(db_session, group_id)
    if group is not None and group.is_default:
        assert_global(user, permission=Permission.FULL_ADMIN_PANEL_ACCESS)
    else:
        assert_manages_group(user, db_session, group_id=group_id)


_SCOPE_DENIAL = (
    "Group managers can only act on private resources within the groups they manage."
)


def _pairs_with_access(
    user: User, db_session: Session, cc_pair_ids: set[int], level: CCPairAccessLevel
) -> set[int]:
    if not cc_pair_ids:
        return set()
    return {
        cc_pair.id
        for cc_pair in get_connector_credential_pairs_for_user(
            db_session, user, level, ids=list(cc_pair_ids)
        )
    }


def _is_connector_admin(user: User) -> bool:
    return (
        has_permission(user, Permission.MANAGE_CONNECTORS) is PermissionAuthority.GLOBAL
    )


def _scope_manage_roles(
    user: User,
    db_session: Session,
    current: dict[int, ConnectorManageRole],
    requested: dict[int, ConnectorManageRole],
) -> dict[int, ConnectorManageRole]:
    """GATE 2 for a group's manage rows. A connector admin sets any row. Anyone
    else gives a role on a pair only if they hold it there (Editor for EDITOR,
    Operator or Editor for OPERATOR), and rows on pairs they cannot operate stay
    as they are."""
    load_cc_pairs(db_session, set(requested))
    if _is_connector_admin(user):
        return requested
    touched = set(current) | set(requested)
    can_edit = _pairs_with_access(user, db_session, touched, CCPairAccessLevel.EDIT)
    can_operate = _pairs_with_access(
        user, db_session, touched, CCPairAccessLevel.OPERATE
    )
    final = {
        cc_pair_id: role
        for cc_pair_id, role in current.items()
        if cc_pair_id not in can_operate
    }
    for cc_pair_id, role in requested.items():
        if current.get(cc_pair_id) == role:
            final[cc_pair_id] = role
            continue
        allowed = can_edit if role is ConnectorManageRole.EDITOR else can_operate
        if cc_pair_id not in allowed:
            raise OnyxError(OnyxErrorCode.INSUFFICIENT_PERMISSIONS, _SCOPE_DENIAL)
        final[cc_pair_id] = role
    return final


def _assert_keeps_authority(db_session: Session, actor: User, group_id: int) -> None:
    """Leaving a group can take away the right that allowed the edit (a manager
    edge, or a grant of this group). Judged on the state after the removal."""
    db_session.flush()
    fresh = fetch_user_by_id(db_session, actor.id)
    if fresh is None:
        return
    db_session.refresh(fresh)
    if not manages_group(fresh, db_session, group_id=group_id):
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            "You can't remove yourself from this group: you would lose the right to "
            "manage it. Ask another admin to do it.",
        )


@router.get("/admin/user-group")
def list_user_groups(
    include_default: bool = Query(False),
    include_users: bool = Query(True),
    user: User = Depends(_read_groups),
    db_session: Session = Depends(get_session),
) -> list[UserGroupSnapshot]:
    visible = get_visible_user_group_ids(user, db_session)
    return _snapshots(
        db_session,
        user,
        None if visible is None else list(visible),
        include_default=include_default,
        include_users=include_users,
    )


@router.get("/admin/user-group/{user_group_id}")
def get_user_group(
    user_group_id: int,
    user: User = Depends(_read_groups),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    visible = get_visible_user_group_ids(user, db_session)
    if visible is not None and user_group_id not in visible:
        raise OnyxError(
            OnyxErrorCode.NOT_FOUND, f"User group {user_group_id} not found."
        )
    return _snapshot(db_session, user, user_group_id)


@router.get("/user-groups/minimal")
def list_minimal_user_groups(
    user: User = Depends(require_permission(Permission.BASIC_ACCESS)),
    db_session: Session = Depends(get_session),
) -> list[MinimalUserGroupSnapshot]:
    """Share targets for pickers: every custom group for a group admin, else the
    custom groups the caller belongs to or manages."""
    groups = fetch_user_groups(db_session, only_up_to_date=False, include_default=False)
    if not has_global_permission(user, Permission.READ_USER_GROUPS):
        own = get_user_group_ids_for_user(
            db_session, user.id
        ) | fetch_managed_group_ids(user, db_session)
        groups = [group for group in groups if group.id in own]
    return [
        MinimalUserGroupSnapshot(id=group.id, name=group.name)
        for group in groups
        if not group.is_up_for_deletion
    ]


@router.get("/admin/permissions/registry")
def get_permission_registry(
    _: User = Depends(_manage_groups_scoped),
) -> list[PermissionRegistryEntry]:
    return PERMISSION_REGISTRY


@router.post("/admin/user-group")
def create_user_group(
    request: UserGroupCreate,
    user: User = Depends(_manage_groups_global),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    group = insert_user_group__no_commit(
        db_session, request.name, request.user_ids, request.cc_pair_ids
    )
    db_session.commit()
    return _snapshot(db_session, user, group.id)


@router.patch("/admin/user-group/rename")
def rename_user_group(
    request: UserGroupRename,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    assert_manages_group(user, db_session, group_id=request.id)
    assert_group_config_is_editable(db_session, request.id, "rename")
    group = fetch_user_group_for_write(db_session, request.id)
    assert_user_group_not_syncing(group)
    rename_user_group__no_commit(db_session, group, request.name)
    db_session.commit()
    return _snapshot(db_session, user, group.id)


@router.patch("/admin/user-group/{user_group_id}")
def update_user_group(
    user_group_id: int,
    request: UserGroupUpdate,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    _assert_may_edit_members(user, db_session, user_group_id)
    group = fetch_user_group_for_write(db_session, user_group_id)
    assert_user_group_not_syncing(group)

    if request.cc_pair_ids is not None:
        current_roles = fetch_current_manage_roles(db_session, group.id)
        requested = set(request.cc_pair_ids)
        if group.is_default and requested:
            raise OnyxError(
                OnyxErrorCode.CONFLICT,
                "Cannot attach connectors to a default system group.",
            )
        roles = _scope_manage_roles(
            user,
            db_session,
            current_roles,
            {
                cc_pair_id: current_roles.get(cc_pair_id, ConnectorManageRole.EDITOR)
                for cc_pair_id in requested
            },
        )
        set_user_group_cc_pairs__no_commit(
            db_session, group, roles, legacy_data_access=True
        )

    changed = update_user_group_members__no_commit(
        db_session, group, user, request.user_ids
    )
    if user.id in changed and user.id not in set(request.user_ids):
        _assert_keeps_authority(db_session, user, group.id)
    db_session.commit()
    return _snapshot(db_session, user, group.id)


@router.post("/admin/user-group/{user_group_id}/add-users")
def add_users_to_user_group(
    user_group_id: int,
    request: AddUsersRequest,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    _assert_may_edit_members(user, db_session, user_group_id)
    group = fetch_user_group_for_write(db_session, user_group_id)
    add_users_to_user_group__no_commit(db_session, group, request.user_ids)
    db_session.commit()
    return _snapshot(db_session, user, group.id)


@router.put("/admin/user-group/{user_group_id}/managed-cc-pairs")
def set_managed_cc_pairs(
    user_group_id: int,
    request: SetManagedCCPairsRequest,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> list[ManagedCCPair]:
    assert_manages_group(user, db_session, group_id=user_group_id)
    assert_group_config_is_editable(db_session, user_group_id, "attach connectors to")
    group = fetch_user_group_for_write(db_session, user_group_id)

    current_roles = fetch_current_manage_roles(db_session, group.id)
    roles = _scope_manage_roles(
        user,
        db_session,
        current_roles,
        {entry.cc_pair_id: entry.role for entry in request.cc_pairs},
    )
    set_user_group_cc_pairs__no_commit(
        db_session, group, roles, legacy_data_access=False
    )
    db_session.commit()
    return [
        ManagedCCPair(cc_pair_id=cc_pair_id, role=role)
        for cc_pair_id, role in sorted(
            fetch_current_manage_roles(db_session, group.id).items()
        )
    ]


@router.put("/admin/user-group/{user_group_id}/manager")
def set_user_group_manager(
    user_group_id: int,
    request: SetManagerRequest,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    assert_manages_group(user, db_session, group_id=user_group_id)
    assert_group_config_is_editable(db_session, user_group_id, "assign a manager in")
    group = fetch_user_group_for_write(db_session, user_group_id)
    set_user_group_manager__no_commit(
        db_session, group, request.user_id, request.is_manager
    )
    if request.user_id == user.id and not request.is_manager:
        _assert_keeps_authority(db_session, user, group.id)
    db_session.commit()
    return _snapshot(db_session, user, group.id)


@router.get("/admin/user-group/{user_group_id}/permissions")
def get_user_group_permissions(
    user_group_id: int,
    include_non_toggleable: bool = Query(False),
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> list[str]:
    assert_manages_group(user, db_session, group_id=user_group_id)
    if fetch_user_group(db_session, user_group_id) is None:
        raise OnyxError(
            OnyxErrorCode.NOT_FOUND, f"User group {user_group_id} not found."
        )
    return fetch_user_group_permissions(
        db_session, user_group_id, include_non_toggleable=include_non_toggleable
    )


@router.put("/admin/user-group/{user_group_id}/permissions")
def set_user_group_permissions(
    user_group_id: int,
    request: SetPermissionsRequest,
    _: User = Depends(_full_admin),
    db_session: Session = Depends(get_session),
) -> list[str]:
    group = fetch_user_group_for_write(db_session, user_group_id)
    permissions = set_user_group_permissions__no_commit(
        db_session, group, request.permissions
    )
    db_session.commit()
    return permissions


@router.patch("/admin/user-group/{user_group_id}/incognito")
def set_user_group_incognito(
    user_group_id: int,
    request: SetIncognitoRequest,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    assert_manages_group(user, db_session, group_id=user_group_id)
    assert_group_config_is_editable(db_session, user_group_id, "change incognito for")
    group = fetch_user_group_for_write(db_session, user_group_id)
    group.incognito_enabled = request.enabled
    db_session.commit()
    return _snapshot(db_session, user, group.id)


@router.put("/admin/user-group/{user_group_id}/data-access-cc-pairs")
def set_data_access_cc_pairs(
    user_group_id: int,
    request: SetDataAccessCCPairsRequest,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> DataAccessCCPairs:
    """Sets the pairs this group can read. A caller who cannot edit a pair
    cannot add it, and its row stays when the request leaves it out."""
    assert_manages_group(user, db_session, group_id=user_group_id)
    assert_group_config_is_editable(db_session, user_group_id, "give data access to")
    group = fetch_user_group_for_write(db_session, user_group_id)
    assert_user_group_not_syncing(group)

    current = fetch_data_access_cc_pair_ids_for_group(db_session, group.id)
    requested = set(request.cc_pair_ids)
    load_cc_pairs(db_session, requested)
    if _is_connector_admin(user):
        editable = current | requested
    else:
        editable = _pairs_with_access(
            user, db_session, current | requested, CCPairAccessLevel.EDIT
        )
        if requested - current - editable:
            raise OnyxError(OnyxErrorCode.INSUFFICIENT_PERMISSIONS, _SCOPE_DENIAL)
    final = (requested & editable) | (current - editable) | (requested & current)

    for cc_pair_id in (current | final) & editable:
        set_cc_pair_data_access_groups__no_commit(
            db_session,
            cc_pair_id=cc_pair_id,
            requested_group_ids={group.id} if cc_pair_id in final else set(),
            # Only this group's row on the pair changes.
            visible_group_ids={group.id},
        )
    assert_restricted_cc_pairs_keep_a_group(db_session, current | final)
    db_session.commit()
    return DataAccessCCPairs(
        cc_pair_ids=sorted(
            fetch_data_access_cc_pair_ids_for_group(db_session, group.id)
        )
    )


@router.patch("/admin/user-group/{user_group_id}/agents")
def set_user_group_agents(
    user_group_id: int,
    request: GroupLinkChange,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    """Shares agents with the group. Adding needs the right to share the agent:
    global MANAGE_AGENTS, or owning it."""
    assert_manages_group(user, db_session, group_id=user_group_id)
    assert_group_config_is_editable(db_session, user_group_id, "share agents with")
    group = fetch_user_group_for_write(db_session, user_group_id)
    if request.added_ids and not has_global_permission(user, Permission.MANAGE_AGENTS):
        owners = fetch_persona_owner_ids(db_session, request.added_ids)
        if any(owners.get(pid) != user.id for pid in request.added_ids):
            raise OnyxError(OnyxErrorCode.INSUFFICIENT_PERMISSIONS, _SCOPE_DENIAL)
    set_user_group_agents__no_commit(
        db_session, group, request.added_ids, request.removed_ids
    )
    for persona_id in {*request.added_ids, *request.removed_ids}:
        mark_persona_user_files_for_sync(persona_id, db_session)
    db_session.commit()
    return _snapshot(db_session, user, group.id)


@router.patch("/admin/user-group/{user_group_id}/document-sets")
def set_user_group_document_sets(
    user_group_id: int,
    request: GroupDocumentSetChange,
    user: User = Depends(_manage_groups_scoped),
    db_session: Session = Depends(get_session),
) -> UserGroupSnapshot:
    """Shares document sets with the group. Adding needs global
    MANAGE_DOCUMENT_SETS."""
    assert_manages_group(user, db_session, group_id=user_group_id)
    assert_group_config_is_editable(
        db_session, user_group_id, "share document sets with"
    )
    group = fetch_user_group_for_write(db_session, user_group_id)
    if request.added_ids:
        assert_global(user, permission=Permission.MANAGE_DOCUMENT_SETS)
    set_user_group_document_sets__no_commit(
        db_session, group, request.added_ids, request.removed_ids
    )
    db_session.commit()
    return _snapshot(db_session, user, group.id)


@router.delete("/admin/user-group/{user_group_id}")
def delete_user_group(
    user_group_id: int,
    user: User = Depends(_manage_groups_global),
    db_session: Session = Depends(get_session),
) -> None:
    assert_group_config_is_editable(db_session, user_group_id, "delete")
    group = fetch_user_group_for_write(db_session, user_group_id)
    assert_user_group_not_syncing(group)
    request_user_group_deletion__no_commit(db_session, group, user)
    db_session.commit()
