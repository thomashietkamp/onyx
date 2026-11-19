"""YouTrack issue ACLs and the group sync that fills them."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from onyx.configs.constants import DocumentSource
from onyx.connectors.youtrack.models import (
    YouTrackAssignedRole,
    YouTrackIssueAccess,
    YouTrackOrganization,
    YouTrackProject,
    YouTrackRole,
    YouTrackUser,
)
from onyx.db.external_perm import ExternalUserGroup, prefixed_group_id
from onyx.external_permissions.youtrack.access import (
    issue_access,
    project_group_id,
    project_node_access,
    project_user_group_id,
    project_visibility_group_id,
)
from onyx.external_permissions.youtrack.group_sync import iter_youtrack_groups

_BASE = "https://yt.example.com"


def _project(pid: str, short: str, helpdesk: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": pid, "shortName": short, "name": short}
    if helpdesk:
        payload["projectType"] = {"id": "HELPDESK"}
    return payload


def _issue(
    project: dict[str, Any],
    visibility: dict[str, Any] | None = None,
    reporter: str | None = None,
) -> YouTrackIssueAccess:
    payload: dict[str, Any] = {"id": "2-1", "project": project}
    if visibility is not None:
        payload["visibility"] = visibility
    if reporter is not None:
        payload["reporter"] = {"id": reporter}
    return YouTrackIssueAccess.model_validate(payload)


def _prefixed(raw: str) -> str:
    return prefixed_group_id(raw, DocumentSource.YOUTRACK)


def test_unrestricted_issue_reads_as_project_group() -> None:
    access = issue_access(_BASE, _issue(_project("0-1", "A")))
    assert access.external_user_group_ids == {_prefixed(project_group_id(_BASE, "0-1"))}
    assert not access.is_public
    assert project_node_access(_BASE, "0-1") == access


def test_restricted_issue_names_visibility_groups_and_users() -> None:
    access = issue_access(
        _BASE,
        _issue(
            _project("0-1", "A"),
            visibility={
                "$type": "LimitedVisibility",
                "permittedGroups": [{"id": "3-7"}],
                "permittedUsers": [{"id": "1-9"}],
            },
            reporter="1-5",
        ),
    )
    assert access.external_user_group_ids == {
        _prefixed(project_visibility_group_id(_BASE, "0-1", "3-7")),
        _prefixed(project_user_group_id(_BASE, "0-1", "1-9")),
    }


def test_helpdesk_reporter_reads_own_ticket() -> None:
    access = issue_access(
        _BASE, _issue(_project("0-2", "HD", helpdesk=True), reporter="1-5")
    )
    assert _prefixed(project_user_group_id(_BASE, "0-2", "1-5")) in (
        access.external_user_group_ids
    )


def test_instances_do_not_share_group_ids() -> None:
    assert project_group_id(_BASE, "0-1") != project_group_id(
        "https://other.example.com", "0-1"
    )


def _user(uid: str, email: str | None, **extra: Any) -> YouTrackUser:
    return YouTrackUser.model_validate({"id": uid, "email": email, **extra})


def _ops(issues: list[YouTrackIssueAccess]) -> MagicMock:
    ops = MagicMock()
    ops.list_projects.return_value = [
        YouTrackProject.model_validate(_project("0-1", "A")),
        YouTrackProject.model_validate(_project("0-2", "HD", helpdesk=True)),
        YouTrackProject.model_validate(_project("0-3", "OLD") | {"archived": True}),
    ]
    ops.list_roles.return_value = [
        YouTrackRole.model_validate(
            {
                "id": "reader",
                "permissions": [{"id": "p", "key": "JetBrains.YouTrack.READ_ISSUE"}],
            }
        ),
        YouTrackRole.model_validate(
            {
                "id": "other",
                "permissions": [{"id": "q", "key": "JetBrains.YouTrack.CREATE_ISSUE"}],
            }
        ),
    ]
    ops.list_assigned_roles.return_value = [
        YouTrackAssignedRole.model_validate(payload)
        for payload in [
            # A group reads every project.
            {
                "role": {"id": "reader"},
                "holder": {"$type": "UserGroup", "id": "g-all"},
                "scope": {"$type": "GlobalScope"},
            },
            # A single user reads project A only.
            {
                "role": {"id": "reader"},
                "holder": {"$type": "User", "id": "u-solo"},
                "scope": {"$type": "ProjectScope", "project": {"id": "0-1"}},
            },
            # The guest reads the helpdesk project through its organization.
            {
                "role": {"id": "reader"},
                "holder": {"$type": "User", "id": "u-guest"},
                "scope": {"$type": "OrganizationScope", "organization": {"id": "org"}},
            },
            # A role without Read Issue grants nothing.
            {
                "role": {"id": "other"},
                "holder": {"$type": "User", "id": "u-outsider"},
                "scope": {"$type": "GlobalScope"},
            },
        ]
    ]
    ops.list_organizations.return_value = [
        YouTrackOrganization.model_validate({"id": "org", "projects": [{"id": "0-2"}]})
    ]
    members = {
        "g-all": {"u-agent", "u-customer", "u-banned"},
        "g-vis": {"u-agent", "u-outsider"},
    }
    ops.list_group_user_ids.side_effect = lambda group_id: members[group_id]
    ops.list_users.return_value = [
        _user("u-agent", "Agent@x.com"),
        _user("u-customer", "cust@x.com", userType={"id": "REPORTER"}),
        _user("u-banned", "banned@x.com", banned=True),
        _user("u-solo", "solo@x.com"),
        _user("u-outsider", "out@x.com"),
        _user("u-guest", None, guest=True),
    ]
    ops.iterate_issue_access.return_value = iter([issues])
    return ops


def _by_id(groups: list[ExternalUserGroup]) -> dict[str, ExternalUserGroup]:
    return {group.id: group for group in groups}


def test_group_sync_fills_project_and_visibility_groups() -> None:
    issues = [
        _issue(
            _project("0-1", "A"),
            visibility={
                "$type": "LimitedVisibility",
                "permittedGroups": [{"id": "g-vis"}],
                "permittedUsers": [{"id": "u-outsider"}],
            },
        ),
        _issue(_project("0-2", "HD", helpdesk=True), reporter="u-customer"),
    ]
    groups = _by_id(list(iter_youtrack_groups(_ops(issues), _BASE, [])))

    project_a = groups[project_group_id(_BASE, "0-1")]
    assert project_a.user_emails == ["agent@x.com", "cust@x.com", "solo@x.com"]
    assert not project_a.gives_anyone_access

    helpdesk = groups[project_group_id(_BASE, "0-2")]
    # Customers are left out; the guest makes the project readable by anyone.
    assert helpdesk.user_emails == ["agent@x.com"]
    assert helpdesk.gives_anyone_access

    # The archived project is out of scope.
    assert project_group_id(_BASE, "0-3") not in groups

    # Only members who can read the project.
    visibility = groups[project_visibility_group_id(_BASE, "0-1", "g-vis")]
    assert visibility.user_emails == ["agent@x.com"]
    outsider = groups[project_user_group_id(_BASE, "0-1", "u-outsider")]
    assert outsider.user_emails == []

    reporter = groups[project_user_group_id(_BASE, "0-2", "u-customer")]
    assert reporter.user_emails == ["cust@x.com"]


def test_group_sync_fails_without_read_issue_role() -> None:
    ops = _ops([])
    ops.list_roles.return_value = []
    with pytest.raises(RuntimeError):
        list(iter_youtrack_groups(ops, _BASE, []))
