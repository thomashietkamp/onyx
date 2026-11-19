"""Confluence page restrictions, space permissions and group sync."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from onyx.connectors.confluence.source_operations import (
    ConfluenceDcSpaceSubjects,
    ConfluenceSpaceNotFoundError,
    ConfluenceSpacePermissionsVariant,
    ConfluenceUserEmailVariant,
)
from onyx.external_permissions.confluence import page_access, space_access
from onyx.external_permissions.confluence.group_sync import (
    ConfluenceGroupMember,
    iter_groups_from_members,
)
from onyx.external_permissions.confluence.page_access import (
    get_page_restrictions,
    get_page_restrictions_with_per_ancestor_fetch,
)
from onyx.external_permissions.confluence.space_access import (
    get_all_space_permissions,
    map_cloud_space_permissions,
)


def _restrictions(
    users: list[dict[str, Any]] | None = None, groups: list[str] | None = None
) -> dict[str, Any]:
    return {
        "read": {
            "restrictions": {
                "user": {"results": users or []},
                "group": {"results": [{"name": name} for name in groups or []]},
            }
        }
    }


def _cloud(func: Any) -> Any:
    return patch.object(page_access, "is_cloud_gateway", return_value=True)(func)


@_cloud
def test_unrestricted_page_uses_the_space(_: MagicMock) -> None:
    assert get_page_restrictions(MagicMock(), "p", _restrictions(), []) is None


@_cloud
def test_page_restriction_maps_users_and_groups(_: MagicMock) -> None:
    access = get_page_restrictions(
        MagicMock(),
        "p",
        _restrictions(
            users=[{"accountId": "1", "email": "Ann@x.com"}, {"accountId": "2"}],
            groups=["Eng"],
        ),
        [],
        add_prefix=True,
    )
    assert access is not None
    assert access.external_user_emails == {"ann@x.com"}
    assert access.external_user_group_ids == {"confluence_eng"}
    assert not access.is_public


@_cloud
def test_ancestor_restriction_is_inherited(_: MagicMock) -> None:
    access = get_page_restrictions(
        MagicMock(),
        "p",
        _restrictions(),
        [{"id": "a", "restrictions": _restrictions(groups=["eng"])}],
    )
    assert access is not None
    assert access.external_user_group_ids == {"eng"}


@_cloud
def test_restrictions_intersect_and_never_widen(_: MagicMock) -> None:
    ann = {"accountId": "1", "email": "ann@x.com"}
    bob = {"accountId": "2", "email": "bob@x.com"}
    access = get_page_restrictions(
        MagicMock(),
        "p",
        # The child names more people than its parent allows.
        _restrictions(users=[ann, bob], groups=["eng", "ops"]),
        [{"id": "a", "restrictions": _restrictions(users=[ann], groups=["eng"])}],
    )
    assert access is not None
    assert access.external_user_emails == {"ann@x.com"}
    assert access.external_user_group_ids == {"eng"}


@_cloud
def test_per_ancestor_fetch_reads_each_ancestor_once(_: MagicMock) -> None:
    ops = MagicMock()
    ops.get_content_read_restrictions.side_effect = lambda content_id: (
        _restrictions(groups=["eng"]) if content_id == "a1" else None
    )
    cache: dict[str, dict[str, Any] | None] = {}
    for _page in range(2):
        access = get_page_restrictions_with_per_ancestor_fetch(
            ops, "p", _restrictions(), [{"id": "a1"}, {"id": "draft"}], cache
        )
        assert access is not None
        assert access.external_user_group_ids == {"eng"}
    assert ops.get_content_read_restrictions.call_count == 2


@patch.object(page_access, "is_cloud_gateway", return_value=False)
def test_dc_restriction_users_are_looked_up(_: MagicMock) -> None:
    ops = MagicMock()
    ops.get_user_email.side_effect = lambda *, variant, user: (
        "Dc@x.com"
        if variant == ConfluenceUserEmailVariant.USERKEY and user == "k1"
        else None
    )
    access = get_page_restrictions(
        ops, "p", _restrictions(users=[{"userKey": "k1", "username": "dc"}]), []
    )
    assert access is not None
    assert access.external_user_emails == {"dc@x.com"}


def test_cloud_space_permissions_read_only_view_grants() -> None:
    emails, groups, is_public = map_cloud_space_permissions(
        MagicMock(),
        [
            {
                "operation": {"operation": "read", "targetType": "space"},
                "subjects": {
                    "user": {"results": [{"accountId": "1", "email": "A@x.com"}]},
                    "group": {"results": [{"name": "confluence-users"}]},
                },
            },
            {
                "operation": {"operation": "administer", "targetType": "space"},
                "subjects": {"group": {"results": [{"name": "admins"}]}},
            },
            {
                "operation": {"operation": "read", "targetType": "space"},
                "anonymousAccess": True,
            },
        ],
    )
    assert emails == {"a@x.com"}
    assert groups == {"confluence-users"}
    assert is_public


def test_unreadable_space_is_left_out() -> None:
    ops = MagicMock()
    ops.list_spaces.return_value = iter([{"key": "OK"}, {"key": "HIDDEN"}])

    def get_space_permissions(
        *, variant: ConfluenceSpacePermissionsVariant, space_key: str
    ) -> list[dict[str, Any]]:
        assert variant == ConfluenceSpacePermissionsVariant.CLOUD
        if space_key == "HIDDEN":
            raise ConfluenceSpaceNotFoundError("not visible")
        return [
            {
                "operation": {"operation": "read", "targetType": "space"},
                "subjects": {"group": {"results": [{"name": "Eng"}]}},
            }
        ]

    ops.get_space_permissions.side_effect = get_space_permissions
    result = get_all_space_permissions(ops, is_cloud=True, add_prefix=True)
    assert set(result) == {"OK"}
    assert result["OK"].external_user_group_ids == {"confluence_eng"}


def test_dc_space_permissions_use_rest_anonymous_call() -> None:
    ops = MagicMock()
    ops.list_spaces.return_value = iter([{"key": "DC"}])
    ops.get_user_email.return_value = "U@x.com"
    ops.get_anonymous_space_permissions.return_value = [
        {"operation": {"operationKey": "read", "targetType": "space"}}
    ]
    subjects = ConfluenceDcSpaceSubjects(
        api=ConfluenceSpacePermissionsVariant.DC_REST,
        users={"key1"},
        groups={"devs"},
        has_anonymous_entry=False,
        raw_permissions=[],
    )
    with patch.object(space_access, "read_dc_space_subjects", return_value=subjects):
        result = get_all_space_permissions(ops, is_cloud=False)
    assert result["DC"].external_user_emails == {"u@x.com"}
    assert result["DC"].external_user_group_ids == {"devs"}
    assert result["DC"].is_public


def _http_error() -> HTTPError:
    return HTTPError("403 Forbidden")


def test_group_sync_inverts_user_groups() -> None:
    members = [
        ConfluenceGroupMember(user_id="1", email="a@x.com"),
        ConfluenceGroupMember(user_id="2", email="b@x.com"),
        ConfluenceGroupMember(user_id="3", email="c@x.com"),
    ]
    memberships = {"1": ["eng", "all"], "2": ["all"]}

    def list_user_groups(user_id: str) -> list[str]:
        if user_id == "3":
            raise _http_error()
        return memberships[user_id]

    groups = {
        group.id: group.user_emails
        for group in iter_groups_from_members(
            members, list_user_groups, enforce_failure_cap=True
        )
    }
    assert groups == {"eng": ["a@x.com"], "all": ["a@x.com", "b@x.com"]}


def test_group_sync_fails_when_most_users_fail() -> None:
    members = [
        ConfluenceGroupMember(user_id=str(i), email=f"{i}@x.com") for i in range(3)
    ]

    def list_user_groups(user_id: str) -> list[str]:
        if user_id != "0":
            raise _http_error()
        return ["eng"]

    with pytest.raises(RuntimeError, match="2 of 3"):
        list(
            iter_groups_from_members(
                members, list_user_groups, enforce_failure_cap=True
            )
        )
    # The Onyx-users fallback skips the cap.
    assert [
        group.id
        for group in iter_groups_from_members(
            members, list_user_groups, enforce_failure_cap=False
        )
    ] == ["eng"]
