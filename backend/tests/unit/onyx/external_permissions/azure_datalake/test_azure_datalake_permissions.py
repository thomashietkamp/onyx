"""Azure Data Lake access: POSIX ACL evaluation, RBAC grants, the walker, and
the Entra group sync."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

from onyx.connectors.azure_datalake.models import (
    AzureDataLakeAccessControl,
    AzureDataLakeContainerAccess,
    AzureDataLakePath,
    AzureDenyAssignment,
    AzureRoleAssignment,
    AzureRoleDefinition,
    AzureStorageAccountPage,
    AzureSubscriptionPage,
)
from onyx.connectors.microsoft_utils.entra import (
    EntraDirectoryObject,
    EntraGroup,
    EntraPage,
    EntraUser,
)
from onyx.external_permissions.azure_datalake.access import (
    BLOB_READ_DATA_ACTION,
    ReaderAlgebra,
    Readers,
    acl_readers,
    build_access_walker,
    rbac_readers,
)
from onyx.external_permissions.azure_datalake.group_sync import (
    iter_azure_datalake_groups,
)

_MEMBERS: dict[str, frozenset[str]] = {
    "g1": frozenset({"u1", "u2"}),
    "g2": frozenset({"u3"}),
}
_ALGEBRA = ReaderAlgebra(lambda group: _MEMBERS.get(group, frozenset()))


def _acl(acl: str, owner: str = "u-owner", group: str = "g-own") -> Any:
    return AzureDataLakeAccessControl(
        owner=owner, group=group, permissions="rwxr-x---+", acl=acl
    )


def test_named_entries_grant_after_mask() -> None:
    readers = acl_readers(
        _acl("user::rwx,user:U5:r--,group::---,group:G1:r-x,mask::r-x,other::---"),
        "r",
        _ALGEBRA,
    )
    assert readers == Readers(
        users=frozenset({"u-owner", "u5"}), groups=frozenset({"g1"})
    )


def test_mask_removes_group_bits() -> None:
    readers = acl_readers(
        _acl("user::---,group:g1:rwx,mask::-wx,other::---"), "r", _ALGEBRA
    )
    assert readers == Readers()


def test_named_user_without_bit_is_cut_from_group() -> None:
    readers = acl_readers(
        _acl("user::rwx,user:u1:---,group:g1:r--,mask::r--,other::---"),
        "r",
        _ALGEBRA,
    )
    # u1 matches its own entry first, so g1 is expanded without it.
    assert readers == Readers(users=frozenset({"u-owner", "u2"}))


def test_other_reaches_everyone_but_matched_classes() -> None:
    readers = acl_readers(
        _acl("user::---,group:g1:---,group:g2:r--,other::r--", group="$superuser"),
        "r",
        _ALGEBRA,
    )
    assert readers.everyone
    assert readers.excluded_users == frozenset({"u-owner", "u1", "u2"})


def test_short_form_without_acl() -> None:
    access_control = AzureDataLakeAccessControl(
        owner="u-owner", group="g-own", permissions="rwxr-x--x", acl=""
    )
    assert acl_readers(access_control, "r", _ALGEBRA) == Readers(
        users=frozenset({"u-owner"}), groups=frozenset({"g-own"})
    )
    assert acl_readers(access_control, "x", _ALGEBRA).everyone


def test_default_entries_are_ignored() -> None:
    readers = acl_readers(
        _acl("user::---,group::---,other::---,default:user:u9:rwx"), "r", _ALGEBRA
    )
    assert readers == Readers()


def test_intersection_expands_only_when_needed() -> None:
    everyone = Readers(everyone=True)
    group = Readers(groups=frozenset({"g1"}))
    assert _ALGEBRA.intersect(everyone, group) == group
    assert _ALGEBRA.intersect(group, group) == group
    assert _ALGEBRA.intersect(group, Readers(users=frozenset({"u2", "u3"}))) == Readers(
        users=frozenset({"u2"})
    )


def _definition(role_id: str, actions: list[str], not_actions: list[str]) -> Any:
    return AzureRoleDefinition.model_validate(
        {
            "id": role_id,
            "properties": {
                "permissions": [{"dataActions": actions, "notDataActions": not_actions}]
            },
        }
    )


def _assignment(aid: str, principal: str, ptype: str, role: str, **extra: Any) -> Any:
    return AzureRoleAssignment.model_validate(
        {
            "id": aid,
            "properties": {
                "principalId": principal,
                "principalType": ptype,
                "roleDefinitionId": role,
                **extra,
            },
        }
    )


_ROLES = {
    "reader": _definition("reader", [BLOB_READ_DATA_ACTION], []),
    "wild": _definition("wild", ["Microsoft.Storage/*"], []),
    "writer-only": _definition(
        "writer-only", ["Microsoft.Storage/*"], [BLOB_READ_DATA_ACTION]
    ),
}


def test_rbac_grants_users_and_groups_with_read() -> None:
    readers = rbac_readers(
        [
            _assignment("a1", "U1", "User", "reader"),
            _assignment("a2", "G2", "Group", "wild"),
            _assignment("a3", "u9", "User", "writer-only"),
            _assignment("a4", "u8", "User", "reader", condition="@Resource[x]"),
            _assignment("a5", "sp", "ServicePrincipal", "reader"),
        ],
        _ROLES.__getitem__,
        [],
        lambda: frozenset(),
        _ALGEBRA,
        "/scope",
    )
    assert readers == Readers(users=frozenset({"u1"}), groups=frozenset({"g2"}))


def test_deny_assignment_removes_principals() -> None:
    deny = AzureDenyAssignment.model_validate(
        {
            "id": "d1",
            "properties": {
                "scope": "/scope",
                "permissions": [{"dataActions": ["*"]}],
                "principals": [{"id": "u1", "type": "User"}],
            },
        }
    )
    readers = rbac_readers(
        [_assignment("a1", "g1", "Group", "reader")],
        _ROLES.__getitem__,
        [deny],
        lambda: frozenset(),
        _ALGEBRA,
        "/scope",
    )
    assert readers == Readers(users=frozenset({"u2"}))


def _path(name: str, is_directory: bool) -> AzureDataLakePath:
    return AzureDataLakePath(
        name=name,
        is_directory=is_directory,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        content_length=1,
    )


def _ops(acls: dict[str, str], anonymous: bool = False) -> MagicMock:
    ops = MagicMock()
    ops.list_users.return_value = EntraPage[EntraUser](
        items=[
            EntraUser.model_validate({"id": "u1", "mail": "One@x.com"}),
            EntraUser.model_validate({"id": "u2", "userPrincipalName": "two@x.com"}),
            EntraUser.model_validate({"id": "u-owner", "mail": "owner@x.com"}),
            EntraUser.model_validate({"id": "u-rbac", "mail": "rbac@x.com"}),
        ]
    )
    ops.list_transitive_group_members.side_effect = lambda group_id, next_link: (  # noqa: ARG005
        EntraPage[EntraDirectoryObject](
            items=[
                EntraDirectoryObject.model_validate(
                    {"id": member, "@odata.type": "#microsoft.graph.user"}
                )
                for member in _MEMBERS.get(group_id, frozenset())
            ]
        )
    )
    ops.list_subscriptions.return_value = AzureSubscriptionPage.model_validate(
        {"value": [{"subscriptionId": "sub"}]}
    )
    ops.list_storage_accounts.return_value = AzureStorageAccountPage.model_validate(
        {
            "value": [
                {"id": "/subscriptions/sub/x/other", "name": "other"},
                {"id": "/subscriptions/sub/x/acct", "name": "acct"},
            ]
        }
    )
    ops.list_role_assignments.return_value = [
        _assignment("a1", "u-rbac", "User", "reader")
    ]
    ops.get_role_definition.side_effect = lambda role_definition_id: _ROLES[
        role_definition_id
    ]
    ops.list_deny_assignments.return_value = []
    ops.get_container_access.return_value = AzureDataLakeContainerAccess(
        anonymous_read=anonymous
    )
    ops.get_access_control.side_effect = lambda path, is_directory: _acl(  # noqa: ARG005
        acls[path]
    )
    return ops


def test_walker_combines_rbac_and_acl_chain() -> None:
    acls = {
        "": "user::rwx,group::---,other::--x",
        "dir": "user::rwx,group::---,group:g1:r-x,mask::r-x,other::---",
        "dir/a.txt": "user::rw-,group::---,group:g1:r--,user:u2:---,mask::r--,other::---",
    }
    ops = _ops(acls)
    walker = build_access_walker(ops, "acct", "box")
    ops.list_role_assignments.assert_called_once_with(
        scope="/subscriptions/sub/x/acct/blobServices/default/containers/box"
    )

    container = walker.container_access()
    assert container.external_user_emails == {"owner@x.com", "rbac@x.com"}

    access = walker.page_access([_path("dir", True), _path("dir/a.txt", False)])
    assert access["dir"].external_user_emails == {"owner@x.com", "rbac@x.com"}
    assert access["dir"].external_user_group_ids == {"g1"}
    # u2 is a named user without read, so g1 is expanded without it.
    assert access["dir/a.txt"].external_user_emails == {
        "owner@x.com",
        "one@x.com",
        "rbac@x.com",
    }
    assert access["dir/a.txt"].external_user_group_ids == set()
    assert not access["dir/a.txt"].is_public
    # Each path's ACL is read once.
    assert ops.get_access_control.call_count == 3


def test_walker_public_when_other_reads_everywhere() -> None:
    acls = {
        "": "user::rwx,group::r-x,other::r-x",
        "f": "user::rw-,group::r--,other::r--",
    }
    walker = build_access_walker(_ops(acls), "acct", "box")
    assert walker.page_access([_path("f", False)])["f"].is_public


def test_walker_public_with_anonymous_container() -> None:
    acls = {
        "": "user::rwx,group::---,other::---",
        "f": "user::rw-,group::---,other::---",
    }
    walker = build_access_walker(_ops(acls, anonymous=True), "acct", "box")
    assert walker.page_access([_path("f", False)])["f"].is_public


def test_group_sync_lists_groups_with_user_emails() -> None:
    ops = MagicMock()
    ops.list_groups.return_value = EntraPage[EntraGroup](
        items=[EntraGroup.model_validate({"id": "G1"})]
    )
    ops.list_transitive_group_members.return_value = EntraPage[EntraDirectoryObject](
        items=[
            EntraDirectoryObject.model_validate(
                {"id": "u1", "@odata.type": "#microsoft.graph.user", "mail": "A@x.com"}
            ),
            EntraDirectoryObject.model_validate(
                {"id": "g2", "@odata.type": "#microsoft.graph.group", "mail": "g@x.com"}
            ),
        ]
    )
    groups = list(iter_azure_datalake_groups(ops))
    assert [(g.id, g.user_emails) for g in groups] == [("g1", ["a@x.com"])]
