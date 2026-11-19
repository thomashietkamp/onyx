"""Group sync for Linear: the readers of each team, and the members of the
workspace. The ids match the ones ``connector.py`` puts on issues.

A team's readers are its active members. A restricted sub-team (a non-private
sub-team of a private team) is also readable by the readers of its parent. The
workspace group holds every active member who is not a guest, since guests
read only the teams they belong to.
"""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from onyx.configs.constants import DocumentSource
from onyx.connectors.credentials_provider import build_db_credentials_provider
from onyx.connectors.linear.models import LinearTeam, TeamVisibility, member_emails
from onyx.connectors.linear.source_operations import LinearSourceOperations
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.external_permissions.linear.connector import (
    team_group_id,
    workspace_group_id,
)


def iter_linear_groups(ops: LinearSourceOperations) -> Iterator[ExternalUserGroup]:
    teams: dict[str, LinearTeam] = {team.id: team for team in ops.list_teams()}
    members: dict[str, set[str]] = {
        team_id: member_emails(ops.list_team_members(team_id=team_id))
        for team_id in teams
    }

    def readers(team_id: str) -> set[str]:
        result: set[str] = set()
        seen: set[str] = set()
        current: str | None = team_id
        while current is not None and current not in seen:
            seen.add(current)
            result |= members.get(current, set())
            team = teams.get(current)
            # A parent the token cannot list adds nobody.
            current = (
                team.parent_id
                if team is not None and team.visibility == TeamVisibility.RESTRICTED
                else None
            )
        return result

    for team_id in teams:
        yield ExternalUserGroup(
            id=team_group_id(team_id), user_emails=sorted(readers(team_id))
        )

    workspace = ops.list_workspace_users()
    yield ExternalUserGroup(
        id=workspace_group_id(workspace.organization_id),
        user_emails=sorted(
            member_emails(user for user in workspace.users if not user.guest)
        ),
    )


def linear_group_sync(
    db_session: Session,  # noqa: ARG001
    cc_pair: ConnectorCredentialPair,
) -> Iterator[ExternalUserGroup]:
    ops = LinearSourceOperations(
        credentials_provider=build_db_credentials_provider(
            DocumentSource.LINEAR, cc_pair.credential.id
        ),
        connector_specific_config=cc_pair.connector.connector_specific_config,
    )
    yield from iter_linear_groups(ops)
