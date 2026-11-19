"""Who may read each Linear issue. The Linear connector reaches this through
``onyx/connectors/linear/connector.py``.

An issue is readable by its team's readers, a group per team that the group
sync fills: the team's members, plus the readers of the parent team for a
restricted sub-team. An issue of a public team is also readable by every
workspace member (guests excluded), a group per workspace. An issue adds the
people it is shared with, and a sub-issue that inherits shared access adds its
parent issue's, up the chain.

Group ids carry the source prefix, because indexing stores access as given;
the doc sync's prefixing leaves a prefixed id as it is.
"""

from collections.abc import Iterator
from typing import Any

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.linear.models import (
    IssueAccess,
    IssueShare,
    LinearTeam,
    TeamVisibility,
)
from onyx.connectors.linear.source_operations import LinearSourceOperations

# A share chain longer than this is a cycle, not a real hierarchy.
_MAX_SHARE_DEPTH = 1_000


def team_group_id(team_id: str) -> str:
    return f"team:{team_id}"


def workspace_group_id(organization_id: str) -> str:
    return f"workspace:{organization_id}"


def _prefixed(group_id: str) -> str:
    return build_ext_group_name_for_onyx(group_id, DocumentSource.LINEAR)


class _ShareResolver:
    """The shared readers of issues, following inherited shares to their
    root. Shares are read once; ancestors outside the walk are fetched."""

    def __init__(self, ops: LinearSourceOperations) -> None:
        self._ops = ops
        self._shares: dict[str, IssueShare] = {}
        self._resolved: dict[str, frozenset[str]] = {}

    def add(self, issue_id: str, share: IssueShare) -> None:
        self._shares.setdefault(issue_id, share)

    def _share(self, issue_id: str) -> IssueShare:
        if issue_id not in self._shares:
            self._shares[issue_id] = self._ops.get_issue_share(issue_id=issue_id)
        return self._shares[issue_id]

    def readers(self, issue_id: str) -> frozenset[str]:
        chain: list[str] = []
        current: str | None = issue_id
        seen: set[str] = set()
        while current is not None and current not in self._resolved:
            if current in seen or len(chain) >= _MAX_SHARE_DEPTH:
                # A cycle grants nothing more than what was read.
                break
            seen.add(current)
            chain.append(current)
            share = self._share(current)
            current = share.parent_id if share.inherits else None
        inherited: frozenset[str] = (
            self._resolved.get(current, frozenset())
            if current is not None
            else frozenset()
        )
        for chain_id in reversed(chain):
            share = self._share(chain_id)
            own = frozenset(email.lower() for email in share.emails)
            inherited = own | inherited if share.inherits else own
            self._resolved[chain_id] = inherited
        return self._resolved[issue_id]


def _team_groups(team: LinearTeam, organization_id: str) -> set[str]:
    groups = {_prefixed(team_group_id(team.id))}
    if team.visibility == TeamVisibility.PUBLIC:
        groups.add(_prefixed(workspace_group_id(organization_id)))
    return groups


def iter_issue_access_pages(
    ops: LinearSourceOperations, issue_filter: dict[str, Any]
) -> Iterator[dict[str, ExternalAccess]]:
    """Per page of the issues the filter matches: who may read each one."""
    resolver = _ShareResolver(ops)
    for page in ops.iterate_issue_access(issue_filter=issue_filter):
        issues: list[IssueAccess] = page.issues
        for issue in issues:
            resolver.add(issue.id, issue.share)
        yield {
            issue.id: ExternalAccess(
                external_user_emails=set(resolver.readers(issue.id)),
                external_user_group_ids=_team_groups(issue.team, page.organization_id),
                is_public=False,
            )
            for issue in issues
        }


def issue_access_by_id(
    ops: LinearSourceOperations, issue_filter: dict[str, Any]
) -> dict[str, ExternalAccess]:
    access: dict[str, ExternalAccess] = {}
    for page in iter_issue_access_pages(ops, issue_filter):
        access.update(page)
    return access
