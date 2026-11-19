"""Group sync for GitLab: fills the groups that ``access.py`` names.

For the pair's project it yields the reporter group (effective access level
Reporter or higher) and the guest group (Guest or higher) from
``/projects/:id/members/all``, which includes inherited and shared-group
members. For an internal project on a self-managed instance it also yields the
instance users group: every active user that the token sees as not external
and not a bot.

GitLab users map to Onyx users by email. GitLab shows ``email`` only to an
admin token, and otherwise the user's public email. A user with neither gets
no access. ``external`` is an admin-only field too, so with a non-admin token
the instance users group stays empty and an internal project is readable only
by its members.
"""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from onyx.connectors.factory import instantiate_connector
from onyx.connectors.gitlab.connector import GitlabConnector
from onyx.connectors.gitlab.models import (
    GUEST_ACCESS_LEVEL,
    REPORTER_ACCESS_LEVEL,
    USER_STATE_ACTIVE,
    GitlabInstanceUser,
)
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.external_permissions.gitlab.access import (
    ROLE_GUEST,
    ROLE_REPORTER,
    instance_users_group_id,
    project_role_group_id,
    uses_instance_users_group,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()


def _is_instance_reader(user: GitlabInstanceUser) -> bool:
    """External users read only projects they are members of, so a user
    counts only when GitLab says it is not external."""
    return user.state == USER_STATE_ACTIVE and not user.bot and user.external is False


def iter_gitlab_groups(connector: GitlabConnector) -> Iterator[ExternalUserGroup]:
    project = connector.fetch_project()
    info = connector.project_access_info(project)
    members = connector.list_project_members(project)

    resolver = connector.email_resolver()
    guest_emails: set[str] = set()
    reporter_emails: set[str] = set()
    for member in members:
        if not member.is_active or member.access_level < GUEST_ACCESS_LEVEL:
            continue
        resolver.remember(member.user_id, member.email)
        email = resolver.email(member.user_id)
        if not email:
            continue
        guest_emails.add(email)
        if member.access_level >= REPORTER_ACCESS_LEVEL:
            reporter_emails.add(email)

    yield ExternalUserGroup(
        id=project_role_group_id(info.instance_url, info.project_id, ROLE_REPORTER),
        user_emails=sorted(reporter_emails),
    )
    yield ExternalUserGroup(
        id=project_role_group_id(info.instance_url, info.project_id, ROLE_GUEST),
        user_emails=sorted(guest_emails),
    )

    if uses_instance_users_group(info):
        users = list(connector.iter_instance_users())
        if users and all(user.external is None for user in users):
            logger.warning(
                "The GitLab token is not an admin: the instance users group of "
                "%s stays empty, so internal project %s is readable only by its "
                "members.",
                info.instance_url,
                info.project_id,
            )
        yield ExternalUserGroup(
            id=instance_users_group_id(info.instance_url),
            user_emails=sorted(
                {
                    user.email
                    for user in users
                    if user.email is not None and _is_instance_reader(user)
                }
            ),
        )


def gitlab_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, GitlabConnector):
        raise RuntimeError("Bug: a GitLab pair built a non-GitLab connector")
    yield from iter_gitlab_groups(connector)
