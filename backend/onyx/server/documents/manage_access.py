"""Who manages a connector (manage roles) and who reads it (data-access groups),
seen from the connector. The group side of the same rows lives in
``onyx/server/user_group/api.py``.

A caller without global MANAGE_CONNECTORS sees and changes only the groups they
manage. An unknown pair and a pair the caller may not edit answer with the same
403, so the route does not reveal which ids exist.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.auth.permissions import (
    PermissionAuthority,
    has_permission,
    require_permission,
)
from onyx.db.cc_pair_data_access import (
    assert_restricted_cc_pairs_keep_a_group,
    fetch_data_access_groups_for_cc_pair,
    set_cc_pair_data_access_groups__no_commit,
)
from onyx.db.connector_credential_pair import (
    CCPairAccessLevel,
    get_connector_credential_pairs_for_user,
)
from onyx.db.connector_manage_access import (
    fetch_fixed_editor_group_ids,
    fetch_group_summaries,
    fetch_manage_roles_for_cc_pair,
    set_cc_pair_manage_roles__no_commit,
)
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import ConnectorManageRole, Permission
from onyx.db.models import User
from onyx.db.scoped_permissions import fetch_managed_group_ids
from onyx.db.user_group import assert_not_shared_with_default_group
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.documents.models import ManageAccessList, manage_access_by_group

router = APIRouter(prefix="/manage/admin/cc-pair")

_manage_connectors = require_permission(Permission.MANAGE_CONNECTORS, allow_scope=True)

_DENIED = (
    "Group managers can only act on private resources within the groups they manage."
)


class CCPairManageAccessRow(BaseModel):
    group_id: int
    group_name: str
    role: ConnectorManageRole
    # Granted by a global connector-management grant: not stored, cannot change.
    is_fixed: bool
    member_count: int
    manager_count: int


class SetManageAccessRequest(BaseModel):
    manage_access: ManageAccessList


class DataAccessGroup(BaseModel):
    id: int
    name: str
    is_default: bool


class CCPairDataAccess(BaseModel):
    # Only the groups the caller can see.
    groups: list[DataAccessGroup]


class SetDataAccessRequest(BaseModel):
    group_ids: list[int]


def _visible_group_ids(user: User, db_session: Session) -> set[int] | None:
    """None: every group (global MANAGE_CONNECTORS)."""
    if has_permission(user, Permission.MANAGE_CONNECTORS) is PermissionAuthority.GLOBAL:
        return None
    return fetch_managed_group_ids(user, db_session)


def _assert_access(
    user: User, db_session: Session, cc_pair_id: int, level: CCPairAccessLevel
) -> None:
    if not get_connector_credential_pairs_for_user(
        db_session, user, level, ids=[cc_pair_id]
    ):
        raise OnyxError(OnyxErrorCode.INSUFFICIENT_PERMISSIONS, _DENIED)


def _manage_rows(
    db_session: Session, cc_pair_id: int, visible: set[int] | None
) -> list[CCPairManageAccessRow]:
    fixed = fetch_fixed_editor_group_ids(db_session)
    stored = {
        group_id: role
        for group_id, role in fetch_manage_roles_for_cc_pair(
            db_session, cc_pair_id
        ).items()
        if group_id not in fixed and (visible is None or group_id in visible)
    }
    summaries = fetch_group_summaries(db_session, fixed | set(stored))
    rows = [
        CCPairManageAccessRow(
            group_id=group_id,
            group_name=summaries[group_id][0],
            role=ConnectorManageRole.EDITOR,
            is_fixed=True,
            member_count=summaries[group_id][1],
            manager_count=summaries[group_id][2],
        )
        for group_id in sorted(fixed)
        if group_id in summaries
    ]
    rows.extend(
        CCPairManageAccessRow(
            group_id=group_id,
            group_name=summaries[group_id][0],
            role=role,
            is_fixed=False,
            member_count=summaries[group_id][1],
            manager_count=summaries[group_id][2],
        )
        for group_id, role in sorted(stored.items())
        if group_id in summaries
    )
    return rows


@router.get("/{cc_pair_id}/manage-access")
def get_manage_access(
    cc_pair_id: int,
    user: User = Depends(_manage_connectors),
    db_session: Session = Depends(get_session),
) -> list[CCPairManageAccessRow]:
    _assert_access(user, db_session, cc_pair_id, CCPairAccessLevel.OPERATE)
    return _manage_rows(db_session, cc_pair_id, _visible_group_ids(user, db_session))


@router.put("/{cc_pair_id}/manage-access")
def set_manage_access(
    cc_pair_id: int,
    request: SetManageAccessRequest,
    user: User = Depends(_manage_connectors),
    db_session: Session = Depends(get_session),
) -> list[CCPairManageAccessRow]:
    """Editors decide who manages the pair. Rows of groups the caller cannot see
    stay; changing one is refused."""
    _assert_access(user, db_session, cc_pair_id, CCPairAccessLevel.EDIT)
    requested = manage_access_by_group(request.manage_access)
    fixed = fetch_fixed_editor_group_ids(db_session) & set(requested)
    if fixed:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            f"Groups {sorted(fixed)} manage every connector already, so they take "
            "no connector role.",
        )
    assert_not_shared_with_default_group(db_session, set(requested))
    missing = set(requested) - set(fetch_group_summaries(db_session, set(requested)))
    if missing:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT, f"User group(s) not found: {sorted(missing)}"
        )

    current = fetch_manage_roles_for_cc_pair(db_session, cc_pair_id)
    visible = _visible_group_ids(user, db_session)
    if visible is not None:
        for group_id in set(current) | set(requested):
            if group_id in visible:
                continue
            if requested.get(group_id, current.get(group_id)) != current.get(group_id):
                raise OnyxError(OnyxErrorCode.INSUFFICIENT_PERMISSIONS, _DENIED)
        requested = {
            **{g: r for g, r in current.items() if g not in visible},
            **requested,
        }
    set_cc_pair_manage_roles__no_commit(db_session, cc_pair_id, requested)
    db_session.commit()
    return _manage_rows(db_session, cc_pair_id, visible)


def _data_access(
    db_session: Session, cc_pair_id: int, visible: set[int] | None
) -> CCPairDataAccess:
    return CCPairDataAccess(
        groups=[
            DataAccessGroup(id=group.id, name=group.name, is_default=group.is_default)
            for group in fetch_data_access_groups_for_cc_pair(db_session, cc_pair_id)
            if visible is None or group.id in visible
        ]
    )


@router.get("/{cc_pair_id}/data-access")
def get_data_access(
    cc_pair_id: int,
    user: User = Depends(_manage_connectors),
    db_session: Session = Depends(get_session),
) -> CCPairDataAccess:
    _assert_access(user, db_session, cc_pair_id, CCPairAccessLevel.OPERATE)
    return _data_access(db_session, cc_pair_id, _visible_group_ids(user, db_session))


@router.put("/{cc_pair_id}/data-access")
def set_data_access(
    cc_pair_id: int,
    request: SetDataAccessRequest,
    user: User = Depends(_manage_connectors),
    db_session: Session = Depends(get_session),
) -> CCPairDataAccess:
    """Data access decides who reads the documents, so it needs Editor."""
    _assert_access(user, db_session, cc_pair_id, CCPairAccessLevel.EDIT)
    visible = _visible_group_ids(user, db_session)
    set_cc_pair_data_access_groups__no_commit(
        db_session,
        cc_pair_id=cc_pair_id,
        requested_group_ids=set(request.group_ids),
        visible_group_ids=visible,
    )
    assert_restricted_cc_pairs_keep_a_group(db_session, [cc_pair_id])
    db_session.commit()
    return _data_access(db_session, cc_pair_id, visible)
