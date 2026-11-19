"""Jira project permissions and group sync."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from onyx.connectors.jira.source_operations import JiraApiError
from onyx.external_permissions.jira import page_access
from onyx.external_permissions.jira.group_sync import iter_jira_groups
from onyx.external_permissions.jira.page_access import get_project_permissions


def _browse(holder: dict[str, Any]) -> dict[str, Any]:
    return {"permission": "BROWSE_PROJECTS", "holder": holder}


def _ops(scheme: dict[str, Any], roles: dict[str, dict[str, Any]]) -> MagicMock:
    ops = MagicMock()
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
    assert access.external_user_group_ids == {"jira_jira-devs", "jira_qa"}
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
    assert access.external_user_group_ids == {"dc-group"}


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
        for group in iter_jira_groups(["devs", "gone", "empty"], member_page)
    }
    assert groups == {"devs": ["a@x.com", "b@x.com"], "empty": []}


def test_group_sync_raises_on_other_errors() -> None:
    def member_page(group_name: str, start_at: int, max_results: int) -> dict:
        del group_name, start_at, max_results
        raise JiraApiError("denied", status_code=403)

    with pytest.raises(JiraApiError):
        list(iter_jira_groups(["devs"], member_page))
