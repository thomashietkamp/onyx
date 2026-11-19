from unittest.mock import MagicMock, patch

from onyx.configs.constants import DocumentSource
from onyx.connectors.linear.connector import LinearConnector
from onyx.connectors.linear.models import (
    IssueAccess,
    IssueAccessPage,
    IssueShare,
    LinearTeam,
    LinearUser,
    TeamVisibility,
    WorkspaceUsers,
)
from onyx.connectors.models import SlimDocument
from onyx.db.external_perm import prefixed_group_id
from onyx.external_permissions.linear.connector import issue_access_by_id
from onyx.external_permissions.linear.group_sync import iter_linear_groups

PUBLIC = LinearTeam(id="t-pub", key="PUB", visibility=TeamVisibility.PUBLIC)
PRIVATE = LinearTeam(id="t-priv", key="PRV", visibility=TeamVisibility.PRIVATE)
RESTRICTED = LinearTeam(
    id="t-sub", key="SUB", visibility=TeamVisibility.RESTRICTED, parent_id="t-priv"
)


def _share(
    emails: set[str], parent_id: str | None = None, inherits: bool = False
) -> IssueShare:
    return IssueShare(parent_id=parent_id, inherits=inherits, emails=emails)


def _user(email: str, guest: bool = False, active: bool = True) -> LinearUser:
    return LinearUser(email=email, active=active, guest=guest, app=False)


def _ops() -> MagicMock:
    ops = MagicMock()
    ops.iterate_issue_access.return_value = iter(
        [
            IssueAccessPage(
                organization_id="org-1",
                issues=[
                    IssueAccess(id="i-1", team=PUBLIC, share=_share(set())),
                    IssueAccess(id="i-2", team=PRIVATE, share=_share({"Shared@x.com"})),
                    # Inherits from i-2 (same walk) and from out-of-walk i-9.
                    IssueAccess(
                        id="i-3",
                        team=RESTRICTED,
                        share=_share({"c@x.com"}, parent_id="i-2", inherits=True),
                    ),
                    IssueAccess(
                        id="i-4",
                        team=PRIVATE,
                        share=_share(set(), parent_id="i-9", inherits=True),
                    ),
                ],
            )
        ]
    )
    ops.get_issue_share.return_value = _share({"far@x.com"})
    ops.list_teams.return_value = [PUBLIC, PRIVATE, RESTRICTED]
    ops.list_team_members.side_effect = lambda team_id: {
        "t-pub": [_user("guest@x.com", guest=True)],
        "t-priv": [_user("p@x.com"), _user("gone@x.com", active=False)],
        "t-sub": [_user("s@x.com")],
    }[team_id]
    ops.list_workspace_users.return_value = WorkspaceUsers(
        organization_id="org-1",
        user_count=3,
        users=[_user("p@x.com"), _user("s@x.com"), _user("guest@x.com", guest=True)],
    )
    return ops


def test_issue_access() -> None:
    ops = _ops()
    access = issue_access_by_id(ops, {})
    assert access["i-1"].external_user_group_ids == {
        "linear_team:t-pub",
        "linear_workspace:org-1",
    }
    assert access["i-1"].external_user_emails == set()
    assert access["i-2"].external_user_group_ids == {"linear_team:t-priv"}
    assert access["i-2"].external_user_emails == {"shared@x.com"}
    assert access["i-3"].external_user_group_ids == {"linear_team:t-sub"}
    assert access["i-3"].external_user_emails == {"c@x.com", "shared@x.com"}
    assert access["i-4"].external_user_emails == {"far@x.com"}
    assert all(not a.is_public for a in access.values())
    ops.get_issue_share.assert_called_once_with(issue_id="i-9")


def test_group_sync_matches_issue_groups() -> None:
    ops = _ops()
    groups = {group.id: group.user_emails for group in iter_linear_groups(ops)}
    assert groups == {
        "team:t-pub": ["guest@x.com"],
        "team:t-priv": ["p@x.com"],
        # A restricted sub-team is readable by its parent's members.
        "team:t-sub": ["p@x.com", "s@x.com"],
        "workspace:org-1": ["p@x.com", "s@x.com"],
    }
    issue_groups = {
        group_id
        for access in issue_access_by_id(_ops(), {}).values()
        for group_id in access.external_user_group_ids
    }
    assert issue_groups == {
        prefixed_group_id(group_id, DocumentSource.LINEAR) for group_id in groups
    }


def test_connector_slim_docs_carry_access() -> None:
    connector = LinearConnector(team_keys=["PRV"])
    ops = _ops()
    with patch.object(LinearConnector, "ops", ops):
        batches = list(connector.retrieve_all_slim_docs_perm_sync())
    docs = [doc for batch in batches for doc in batch if isinstance(doc, SlimDocument)]
    assert len(docs) == sum(len(batch) for batch in batches)
    assert [doc.id for doc in docs] == ["i-1", "i-2", "i-3", "i-4"]
    assert all(doc.external_access is not None for doc in docs)
    issue_filter = ops.iterate_issue_access.call_args.kwargs["issue_filter"]
    assert issue_filter["team"] == {"key": {"in": ["PRV"]}}
