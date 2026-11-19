"""Jira project permissions and group sync."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.jira.config import JiraCredentialBinding
from onyx.connectors.jira.source_operations import JiraApiError, bound_site_url
from onyx.external_permissions.jira import page_access
from onyx.external_permissions.jira.group_sync import iter_jira_groups
from onyx.external_permissions.jira.page_access import (
    get_project_permissions,
    jira_group_id,
)

_SITE = "https://acme.atlassian.net"
_OTHER_SITE = "https://jira.example.com"


def _prefixed(group_id: str) -> str:
    return build_ext_group_name_for_onyx(group_id, DocumentSource.JIRA)


def _browse(holder: dict[str, Any]) -> dict[str, Any]:
    return {"permission": "BROWSE_PROJECTS", "holder": holder}


def _ops(
    scheme: dict[str, Any], roles: dict[str, dict[str, Any]], site: str = _SITE
) -> MagicMock:
    ops = MagicMock()
    ops._binding.return_value = JiraCredentialBinding(jira_base_url=site)
    ops.get_project_permission_scheme.return_value = scheme
    ops.get_project_role.side_effect = lambda **kwargs: roles[kwargs["role_id"]]
    ops.get_user.side_effect = lambda *, user_id: {
        "acc-2": {"accountType": "atlassian", "emailAddress": "Bob@x.com"},
        "app-1": {"accountType": "app", "emailAddress": "bot@x.com"},
    }[user_id]
    return ops


@patch.object(page_access, "is_cloud_gateway", return_value=True)
def test_scheme_maps_users_groups_and_roles(_: MagicMock) -> None:
    scheme = {
        "permissions": [
            _browse(
                {
                    "type": "user",
                    "parameter": "acc-1",
                    "user": {"accountId": "acc-1", "emailAddress": "Ann@x.com"},
                }
            ),
            _browse({"type": "group", "parameter": "jira-devs"}),
            _browse({"type": "projectRole", "parameter": "10002"}),
            _browse({"type": "reporter"}),
            # Another permission adds no one.
            {"permission": "ADMINISTER_PROJECTS", "holder": {"type": "group"}},
        ]
    }
    roles = {
        "10002": {
            "actors": [
                {"actorUser": {"accountId": "acc-2"}},
                {"actorUser": {"accountId": "app-1"}},
                {"actorGroup": {"name": "qa", "groupId": "g-1"}},
            ]
        }
    }
    access = get_project_permissions(_ops(scheme, roles), "PRJ", add_prefix=True)
    assert access is not None
    assert access.external_user_emails == {"ann@x.com", "bob@x.com"}
    assert access.external_user_group_ids == {
        _prefixed(jira_group_id(_SITE, "jira-devs")),
        _prefixed(jira_group_id(_SITE, "qa")),
    }
    assert not access.is_public


@patch.object(page_access, "is_cloud_gateway", return_value=False)
def test_dc_role_actors_are_flat(_: MagicMock) -> None:
    scheme = {"permissions": [_browse({"type": "projectRole", "parameter": "1"})]}
    roles = {
        "1": {
            "actors": [
                {"type": "atlassian-user-role-actor", "name": "dc-user"},
                {"type": "atlassian-group-role-actor", "name": "dc-group"},
            ]
        }
    }
    ops = _ops(scheme, roles)
    ops.get_user.side_effect = lambda *, user_id: {"emailAddress": f"{user_id}@x.com"}
    access = get_project_permissions(ops, "PRJ")
    assert access is not None
    assert access.external_user_emails == {"dc-user@x.com"}
    assert access.external_user_group_ids == {jira_group_id(_SITE, "dc-group")}


@pytest.mark.parametrize("holder_type", ["anyone", "applicationRole"])
@patch.object(page_access, "is_cloud_gateway", return_value=True)
def test_open_projects_are_public(_: MagicMock, holder_type: str) -> None:
    scheme = {"permissions": [_browse({"type": holder_type})]}
    access = get_project_permissions(_ops(scheme, {}), "PRJ")
    assert access is not None
    assert access.is_public


@patch.object(page_access, "is_cloud_gateway", return_value=True)
def test_unreadable_role_keeps_last_acl(_: MagicMock) -> None:
    scheme = {"permissions": [_browse({"type": "projectRole", "parameter": "1"})]}
    ops = _ops(scheme, {})
    ops.get_project_role.side_effect = JiraApiError("denied", status_code=403)
    assert get_project_permissions(ops, "PRJ") is None


def test_group_sync_pages_members_and_skips_deleted_groups() -> None:
    pages = {
        ("devs", 0): {
            "values": [
                {"accountType": "atlassian", "emailAddress": "A@x.com"},
                {"accountType": "app", "emailAddress": "bot@x.com"},
            ],
            "isLast": False,
        },
        ("devs", 2): {
            "values": [{"accountType": "atlassian", "emailAddress": "b@x.com"}],
            "isLast": True,
        },
        ("empty", 0): {"values": [], "isLast": True},
    }

    def member_page(group_name: str, start_at: int, max_results: int) -> dict:
        del max_results
        if group_name == "gone":
            raise JiraApiError(
                "missing", status_code=404, body="Group 'gone' does not exist"
            )
        return pages[(group_name, start_at)]

    groups = {
        group.id: group.user_emails
        for group in iter_jira_groups(
            ["devs", "gone", "empty"], member_page, site_url=_SITE
        )
    }
    assert groups == {
        jira_group_id(_SITE, "devs"): ["a@x.com", "b@x.com"],
        jira_group_id(_SITE, "empty"): [],
    }


def test_group_sync_raises_on_other_errors() -> None:
    def member_page(group_name: str, start_at: int, max_results: int) -> dict:
        del group_name, start_at, max_results
        raise JiraApiError("denied", status_code=403)

    with pytest.raises(JiraApiError):
        list(iter_jira_groups(["devs"], member_page, site_url=_SITE))


_GROUP_SCHEME = {"permissions": [_browse({"type": "group", "parameter": "jira-devs"})]}


def _members(group_name: str, start_at: int, max_results: int) -> dict:
    del group_name, start_at, max_results
    return {"values": [{"emailAddress": "a@x.com"}], "isLast": True}


@patch.object(page_access, "is_cloud_gateway", return_value=True)
def test_document_and_group_sync_ids_match_for_one_site(_: MagicMock) -> None:
    ops = _ops(_GROUP_SCHEME, {})
    access = get_project_permissions(ops, "PRJ")
    assert access is not None
    synced = {
        group.id
        for group in iter_jira_groups(
            ["jira-devs"], _members, site_url=bound_site_url(ops)
        )
    }
    assert access.external_user_group_ids == synced


@patch.object(page_access, "is_cloud_gateway", return_value=True)
def test_same_group_name_on_two_sites_names_different_groups(_: MagicMock) -> None:
    on_a = get_project_permissions(_ops(_GROUP_SCHEME, {}, _SITE), "PRJ")
    on_b = get_project_permissions(_ops(_GROUP_SCHEME, {}, _OTHER_SITE), "PRJ")
    assert on_a is not None and on_b is not None
    assert on_a.external_user_group_ids
    assert not on_a.external_user_group_ids & on_b.external_user_group_ids
