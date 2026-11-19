"""Confluence page restrictions, space permissions and group sync."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.confluence.config import ConfluenceCredentialBinding
from onyx.connectors.confluence.source_operations import (
    ConfluenceDcSpaceSubjects,
    ConfluenceSpaceNotFoundError,
    ConfluenceSpacePermissionsVariant,
    ConfluenceUserEmailVariant,
    bound_site_url,
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
from onyx.external_permissions.confluence.users import confluence_group_id

_SITE = "https://acme.atlassian.net/wiki"
# The site URL that ``bound_site_url`` gives for ``_SITE``.
_SITE_KEY_URL = "https://acme.atlassian.net"
_OTHER_SITE = "https://other.atlassian.net/wiki"


def _ops(wiki_base: str = _SITE, is_cloud: bool = True) -> MagicMock:
    """A gateway bound to ``wiki_base``."""
    ops = MagicMock()
    ops._binding.return_value = ConfluenceCredentialBinding(
        wiki_base=wiki_base, is_cloud=is_cloud
    )
    return ops


def _group(name: str) -> str:
    return confluence_group_id(_SITE_KEY_URL, name)


def _prefixed(name: str) -> str:
    return build_ext_group_name_for_onyx(_group(name), DocumentSource.CONFLUENCE)


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
    assert get_page_restrictions(_ops(), "p", _restrictions(), []) is None


@_cloud
def test_page_restriction_maps_users_and_groups(_: MagicMock) -> None:
    access = get_page_restrictions(
        _ops(),
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
    assert access.external_user_group_ids == {_prefixed("eng")}
    assert not access.is_public


@_cloud
def test_ancestor_restriction_is_inherited(_: MagicMock) -> None:
    access = get_page_restrictions(
        _ops(),
        "p",
        _restrictions(),
        [{"id": "a", "restrictions": _restrictions(groups=["eng"])}],
    )
    assert access is not None
    assert access.external_user_group_ids == {_group("eng")}


@_cloud
def test_restrictions_intersect_and_never_widen(_: MagicMock) -> None:
    ann = {"accountId": "1", "email": "ann@x.com"}
    bob = {"accountId": "2", "email": "bob@x.com"}
    access = get_page_restrictions(
        _ops(),
        "p",
        # The child names more people than its parent allows.
        _restrictions(users=[ann, bob], groups=["eng", "ops"]),
        [{"id": "a", "restrictions": _restrictions(users=[ann], groups=["eng"])}],
    )
    assert access is not None
    assert access.external_user_emails == {"ann@x.com"}
    assert access.external_user_group_ids == {_group("eng")}


@_cloud
def test_per_ancestor_fetch_reads_each_ancestor_once(_: MagicMock) -> None:
    ops = _ops()
    ops.get_content_read_restrictions.side_effect = lambda content_id: (
        _restrictions(groups=["eng"]) if content_id == "a1" else None
    )
    cache: dict[str, dict[str, Any] | None] = {}
    for _page in range(2):
        access = get_page_restrictions_with_per_ancestor_fetch(
            ops, "p", _restrictions(), [{"id": "a1"}, {"id": "draft"}], cache
        )
        assert access is not None
        assert access.external_user_group_ids == {_group("eng")}
    assert ops.get_content_read_restrictions.call_count == 2


@patch.object(page_access, "is_cloud_gateway", return_value=False)
def test_dc_restriction_users_are_looked_up(_: MagicMock) -> None:
    ops = _ops(is_cloud=False)
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
    ops = _ops()
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
    assert result["OK"].external_user_group_ids == {_prefixed("eng")}


def test_dc_space_permissions_use_rest_anonymous_call() -> None:
    ops = _ops("https://wiki.example.com/", is_cloud=False)
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
    assert result["DC"].external_user_group_ids == {
        confluence_group_id("https://wiki.example.com", "devs")
    }
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
            members, list_user_groups, site_url=_SITE_KEY_URL, enforce_failure_cap=True
        )
    }
    assert groups == {
        _group("eng"): ["a@x.com"],
        _group("all"): ["a@x.com", "b@x.com"],
    }


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
                members,
                list_user_groups,
                site_url=_SITE_KEY_URL,
                enforce_failure_cap=True,
            )
        )
    # The Onyx-users fallback skips the cap.
    assert [
        group.id
        for group in iter_groups_from_members(
            members, list_user_groups, site_url=_SITE_KEY_URL, enforce_failure_cap=False
        )
    ] == [_group("eng")]


def _space_read(group_name: str) -> list[dict[str, Any]]:
    return [
        {
            "operation": {"operation": "read", "targetType": "space"},
            "subjects": {"group": {"results": [{"name": group_name}]}},
        }
    ]


def _space_group_ids(ops: MagicMock) -> set[str]:
    ops.list_spaces.return_value = iter([{"key": "ENG"}])
    ops.get_space_permissions.return_value = _space_read("confluence-users")
    return get_all_space_permissions(ops, is_cloud=True)["ENG"].external_user_group_ids


@_cloud
def test_document_and_group_sync_ids_match_for_one_site(_: MagicMock) -> None:
    ops = _ops()
    space_ids = _space_group_ids(ops)
    page = get_page_restrictions(
        ops, "p", _restrictions(groups=["confluence-users"]), []
    )
    assert page is not None
    synced = {
        group.id
        for group in iter_groups_from_members(
            [ConfluenceGroupMember(user_id="1", email="a@x.com")],
            lambda _user_id: ["confluence-users"],
            site_url=bound_site_url(ops),
            enforce_failure_cap=True,
        )
    }
    assert space_ids == page.external_user_group_ids == synced


@_cloud
def test_same_group_name_on_two_sites_names_different_groups(_: MagicMock) -> None:
    """Members of ``confluence-users`` on one site must not read the content
    of another site that is restricted to its own ``confluence-users``."""
    space_a = _space_group_ids(_ops(_SITE))
    space_b = _space_group_ids(_ops(_OTHER_SITE))
    assert space_a and space_b
    assert not space_a & space_b
    page_a = get_page_restrictions(
        _ops(_SITE), "p", _restrictions(groups=["developers"]), []
    )
    page_b = get_page_restrictions(
        _ops(_OTHER_SITE), "p", _restrictions(groups=["developers"]), []
    )
    assert page_a is not None and page_b is not None
    assert not page_a.external_user_group_ids & page_b.external_user_group_ids


def test_cloud_site_with_and_without_wiki_path_is_one_site() -> None:
    assert bound_site_url(_ops("https://acme.atlassian.net/wiki/")) == bound_site_url(
        _ops("https://ACME.atlassian.net")
    )
