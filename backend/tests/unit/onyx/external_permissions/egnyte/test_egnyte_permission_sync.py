from unittest.mock import MagicMock

import pytest

from onyx.configs.constants import DocumentSource
from onyx.connectors.egnyte.access import EgnyteUnknownGroupError
from onyx.connectors.egnyte.connector import _prefixed
from onyx.connectors.egnyte.models import (
    EgnyteDirectory,
    EgnyteGroup,
    EgnyteGroupMember,
    EgnyteGroupRef,
    EgnytePermissionGrant,
    EgnytePermissions,
    EgnyteUser,
    EgnyteUserType,
)
from onyx.db.external_perm import prefixed_group_id
from onyx.external_permissions.egnyte.access import build_directory, folder_access
from onyx.external_permissions.egnyte.group_sync import iter_egnyte_groups


def _user(name: str, user_type: EgnyteUserType, active: bool = True) -> EgnyteUser:
    return EgnyteUser(
        user_name=name,
        email=f"{name.upper()}@Acme.com",
        user_type=user_type,
        active=active,
    )


def _ops() -> MagicMock:
    ops = MagicMock()
    ops.list_users.return_value = [
        _user("ann", EgnyteUserType.ADMIN),
        _user("bob", EgnyteUserType.POWER),
        _user("cat", EgnyteUserType.STANDARD),
        _user("dan", EgnyteUserType.STANDARD, active=False),
    ]
    ops.list_groups.return_value = [EgnyteGroupRef(id="g-1", display_name="Finance")]
    ops.get_group.return_value = EgnyteGroup(
        id="g-1",
        display_name="Finance",
        members=[
            EgnyteGroupMember(username="Bob"),
            EgnyteGroupMember(username="dan"),
            EgnyteGroupMember(username="ghost"),
        ],
    )
    return ops


def _grant(subject: str, permission: str = "Viewer") -> EgnytePermissionGrant:
    return EgnytePermissionGrant(subject=subject, permission=permission)


def test_folder_access_maps_users_and_groups() -> None:
    directory = build_directory(_ops())
    access = folder_access(
        "Acme",
        EgnytePermissions(
            users=[_grant("Cat", "Editor"), _grant("dan"), _grant("bob", "None")],
            groups=[_grant("finance", "Full"), _grant("All Power Users")],
        ),
        directory,
    )
    # dan is inactive, bob's grant is "None".
    assert access.external_user_emails == {"cat@acme.com"}
    assert access.external_user_group_ids == {
        "acme:g-1",
        "acme:default-all-power-users",
    }
    assert access.is_public is False


def test_folder_without_grants_is_private() -> None:
    access = folder_access(
        "acme", None, EgnyteDirectory(emails_by_username={}, group_ids_by_name={})
    )
    assert access.external_user_emails == set()
    assert access.external_user_group_ids == set()
    assert access.is_public is False


def test_unknown_group_raises() -> None:
    with pytest.raises(EgnyteUnknownGroupError):
        folder_access(
            "acme",
            EgnytePermissions(groups=[_grant("Nobody Knows")]),
            build_directory(_ops()),
        )


def test_group_sync_ids_match_folder_access() -> None:
    ops = _ops()
    groups = {group.id: group.user_emails for group in iter_egnyte_groups(ops, "Acme")}
    assert groups == {
        "acme:default-all-power-users": ["ann@acme.com", "bob@acme.com"],
        "acme:default-all-standard-users": ["cat@acme.com"],
        "acme:default-all-users": ["ann@acme.com", "bob@acme.com", "cat@acme.com"],
        "acme:g-1": ["bob@acme.com"],
    }
    access = folder_access(
        "acme",
        EgnytePermissions(groups=[_grant("Finance")]),
        build_directory(ops),
    )
    # The indexing path stores the connector-prefixed id; the group rows and
    # the doc sync store the same form.
    indexed = _prefixed(access).external_user_group_ids
    assert indexed == {prefixed_group_id("acme:g-1", DocumentSource.EGNYTE)}
    assert {
        prefixed_group_id(group_id, DocumentSource.EGNYTE)
        for group_id in access.external_user_group_ids
    } == indexed
