"""Group sync for YouTrack: fills the groups that ``access.py`` names.

A user can read a project's issues when a role with the Read Issue permission
is granted to the user, or to a group the user is in, in the global scope, in
an organization that holds the project, or in the project. Banned users read
nothing. A grant that reaches the guest user lets anyone read the project.
"""

from collections.abc import Callable, Iterator

from sqlalchemy.orm import Session

from onyx.connectors.factory import instantiate_connector
from onyx.connectors.youtrack.connector import (
    YouTrackConnector,
    active_projects,
    scoped_query,
)
from onyx.connectors.youtrack.models import (
    USER_HOLDER_TYPE,
    ScopeType,
    YouTrackAssignedRole,
    YouTrackOrganization,
    YouTrackProject,
    YouTrackRole,
    YouTrackUser,
)
from onyx.connectors.youtrack.source_operations import YouTrackSourceOperations
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.external_permissions.youtrack.access import (
    project_group_id,
    project_user_group_id,
    project_visibility_group_id,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

READ_ISSUE_PERMISSION = "READ_ISSUE"


def _grants_read_issue(role: YouTrackRole) -> bool:
    for permission in role.permissions:
        key: str = (permission.key or permission.id).upper()
        if key == READ_ISSUE_PERMISSION or key.endswith(f".{READ_ISSUE_PERMISSION}"):
            return True
    return False


def project_readers(
    projects: list[YouTrackProject],
    roles: list[YouTrackRole],
    assigned_roles: list[YouTrackAssignedRole],
    organizations: list[YouTrackOrganization],
    group_members: Callable[[str], set[str]],
) -> dict[str, set[str]]:
    """Project id to the ids of the users with Read Issue in the project."""
    read_roles: set[str] = {role.id for role in roles if _grants_read_issue(role)}
    if not read_roles:
        raise RuntimeError(
            "No YouTrack role has the Read Issue permission, or the token cannot "
            "read the roles' permissions."
        )
    org_projects: dict[str, set[str]] = {
        org.id: {ref.id for ref in org.projects} for org in organizations
    }
    project_ids: set[str] = {project.id for project in projects}
    readers: dict[str, set[str]] = {project_id: set() for project_id in project_ids}

    for grant in assigned_roles:
        if grant.role.id not in read_roles:
            continue
        scope_type: str = grant.scope.type
        if scope_type == ScopeType.GLOBAL.value:
            targets: set[str] = project_ids
        elif scope_type == ScopeType.ORGANIZATION.value:
            organization = grant.scope.organization
            targets = (
                org_projects.get(organization.id, set()) & project_ids
                if organization is not None
                else set()
            )
        elif scope_type == ScopeType.PROJECT.value:
            project = grant.scope.project
            targets = {project.id} & project_ids if project is not None else set()
        else:
            logger.warning("Skipping a YouTrack grant in scope %s", scope_type)
            continue
        if not targets:
            continue
        users: set[str] = (
            {grant.holder.id}
            if grant.holder.type == USER_HOLDER_TYPE
            else group_members(grant.holder.id)
        )
        for project_id in targets:
            readers[project_id] |= users
    return readers


def _group(
    group_id: str, user_ids: set[str], users: dict[str, YouTrackUser]
) -> ExternalUserGroup:
    emails: set[str] = set()
    anyone = False
    for user_id in user_ids:
        user = users.get(user_id)
        if user is None or user.banned:
            continue
        if user.guest:
            anyone = True
            continue
        if user.email:
            emails.add(user.email.strip().lower())
    return ExternalUserGroup(
        id=group_id, user_emails=sorted(emails), gives_anyone_access=anyone
    )


def iter_youtrack_groups(
    ops: YouTrackSourceOperations, base_url: str, projects: list[str]
) -> Iterator[ExternalUserGroup]:
    """Every group an issue or project node in scope can name."""
    in_scope: list[YouTrackProject] = active_projects(ops, projects)
    if not in_scope:
        return
    members_cache: dict[str, set[str]] = {}

    def group_members(group_id: str) -> set[str]:
        if group_id not in members_cache:
            members_cache[group_id] = ops.list_group_user_ids(group_id=group_id)
        return members_cache[group_id]

    readers: dict[str, set[str]] = project_readers(
        in_scope,
        ops.list_roles(),
        ops.list_assigned_roles(),
        ops.list_organizations(),
        group_members,
    )
    users: dict[str, YouTrackUser] = {user.id: user for user in ops.list_users()}
    helpdesk: set[str] = {project.id for project in in_scope if project.is_helpdesk}

    def agents(project_id: str, user_ids: set[str]) -> set[str]:
        """Customers read only their own tickets in a helpdesk project."""
        if project_id not in helpdesk:
            return user_ids
        return {
            user_id
            for user_id in user_ids
            if (user := users.get(user_id)) is not None and not user.is_customer
        }

    for project in in_scope:
        yield _group(
            project_group_id(base_url, project.id),
            agents(project.id, readers[project.id]),
            users,
        )

    query: str | None = scoped_query(ops, projects)
    if query is None:
        return
    visibility_groups: set[tuple[str, str]] = set()
    single_users: set[tuple[str, str]] = set()
    for page in ops.iterate_issue_access(query=query):
        for issue in page:
            project_id: str = issue.project.id
            if project_id not in readers:
                continue
            visibility = issue.visibility
            if visibility is not None and visibility.is_restricted:
                visibility_groups.update(
                    (project_id, group.id) for group in visibility.permitted_groups
                )
                single_users.update(
                    (project_id, user.id) for user in visibility.permitted_users
                )
            if issue.project.is_helpdesk and issue.reporter is not None:
                single_users.add((project_id, issue.reporter.id))

    for project_id, group_id in sorted(visibility_groups):
        yield _group(
            project_visibility_group_id(base_url, project_id, group_id),
            agents(project_id, group_members(group_id) & readers[project_id]),
            users,
        )
    for project_id, user_id in sorted(single_users):
        yield _group(
            project_user_group_id(base_url, project_id, user_id),
            {user_id} & readers[project_id],
            users,
        )


def youtrack_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, YouTrackConnector):
        raise RuntimeError("Bug: a YouTrack pair built a non-YouTrack connector")
    yield from iter_youtrack_groups(
        connector.ops, connector.base_url, connector.projects
    )
