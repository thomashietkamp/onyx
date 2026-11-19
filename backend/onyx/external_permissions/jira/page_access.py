"""Who may read the issues of a Jira project, from the Browse Projects grants of
the project's permission scheme. The connector reaches this through
``onyx/connectors/jira/access.py`` and caches it per project.

- ``anyone`` and ``applicationRole`` (every user with product access) make the
  project public.
- ``user`` adds the user's email.
- ``group`` adds the group name, whose members the group sync supplies.
- ``projectRole`` adds the role's user actors (emails) and group actors.
- Other holders (project lead, reporter, assignee, user custom fields) are
  issue-relative or rare and add no one: their users can lose access in Onyx,
  but no one gains access that Jira denies.

Reading the scheme needs Administer Projects on the project, or Administer
Jira. When the scheme or a role cannot be read, the result is None, and a doc
sync keeps the issues' last synced ACL.
"""

from typing import Any

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.jira.source_operations import (
    JiraApiError,
    JiraSourceOperations,
    is_cloud_gateway,
)
from onyx.connectors.jira.utils import (
    ATLASSIAN_GROUP_ROLE_ACTOR_TYPE,
    ATLASSIAN_USER_ROLE_ACTOR_TYPE,
    BROWSE_PROJECTS_PERMISSION,
    HOLDER_TYPE_ANYONE,
    HOLDER_TYPE_APPLICATION_ROLE,
    HOLDER_TYPE_GROUP,
    HOLDER_TYPE_PROJECT_ROLE,
    HOLDER_TYPE_USER,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

_PUBLIC_HOLDER_TYPES = frozenset({HOLDER_TYPE_ANYONE, HOLDER_TYPE_APPLICATION_ROLE})
_ATLASSIAN_ACCOUNT_TYPE = "atlassian"
_NOT_FOUND = 404


class _UserEmails:
    """Resolves Jira users to emails, one lookup per user per call."""

    def __init__(self, source_operations: JiraSourceOperations) -> None:
        self._ops = source_operations
        self._is_cloud = is_cloud_gateway(source_operations)
        self._cache: dict[str, str | None] = {}

    def _lookup_id(self, user: dict[str, Any]) -> str | None:
        fields = ("accountId", "name", "key") if self._is_cloud else ("name", "key")
        for field in fields:
            value = user.get(field)
            if isinstance(value, str) and value:
                return value
        return None

    def email(self, user: dict[str, Any]) -> str | None:
        account_type = user.get("accountType")
        if account_type is not None and account_type != _ATLASSIAN_ACCOUNT_TYPE:
            # Apps and customers never map to Onyx users.
            return None
        inline = user.get("emailAddress")
        if isinstance(inline, str) and inline:
            return inline.lower()
        lookup_id = self._lookup_id(user)
        if lookup_id is None:
            return None
        if lookup_id not in self._cache:
            self._cache[lookup_id] = self._fetch(lookup_id)
        return self._cache[lookup_id]

    def _fetch(self, lookup_id: str) -> str | None:
        try:
            details = self._ops.get_user(user_id=lookup_id)
        except JiraApiError as e:
            if e.status_code != _NOT_FOUND:
                raise
            return None
        account_type = details.get("accountType")
        if account_type is not None and account_type != _ATLASSIAN_ACCOUNT_TYPE:
            return None
        email = details.get("emailAddress")
        return email.lower() if isinstance(email, str) and email else None


def browse_holders(scheme: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        permission["holder"]
        for permission in scheme.get("permissions") or []
        if isinstance(permission, dict)
        and permission.get("permission") == BROWSE_PROJECTS_PERMISSION
        and isinstance(permission.get("holder"), dict)
    ]


def _holder_group_name(holder: dict[str, Any]) -> str | None:
    group = holder.get("group")
    if isinstance(group, dict):
        name = group.get("name")
        if isinstance(name, str) and name:
            return name
    parameter = holder.get("parameter")
    return parameter if isinstance(parameter, str) and parameter else None


def _role_id(holder: dict[str, Any]) -> str | None:
    role_id = holder.get("value") or holder.get("parameter")
    return str(role_id) if role_id else None


def _holder_user(holder: dict[str, Any]) -> dict[str, Any]:
    user = holder.get("user")
    if isinstance(user, dict):
        return user
    parameter = holder.get("parameter") or holder.get("value")
    # The holder names the user by accountId (Cloud) or username (DC).
    return {"accountId": parameter, "name": parameter} if parameter else {}


def _role_actor_user(actor: dict[str, Any]) -> dict[str, Any] | None:
    actor_user = actor.get("actorUser")
    if isinstance(actor_user, dict):
        return actor_user
    if actor.get("type") == ATLASSIAN_USER_ROLE_ACTOR_TYPE:
        return actor
    return None


def _role_actor_group(actor: dict[str, Any]) -> str | None:
    actor_group = actor.get("actorGroup")
    if isinstance(actor_group, dict):
        name = actor_group.get("name")
        return name if isinstance(name, str) and name else None
    if actor.get("type") == ATLASSIAN_GROUP_ROLE_ACTOR_TYPE:
        name = actor.get("name")
        return name if isinstance(name, str) and name else None
    return None


def map_project_permissions(
    source_operations: JiraSourceOperations,
    project_key: str,
    scheme: dict[str, Any],
    *,
    add_prefix: bool,
) -> ExternalAccess:
    """Maps a permission scheme to an ``ExternalAccess``. Raises
    ``JiraApiError`` when a role or a user cannot be read."""
    holders = browse_holders(scheme)
    if any(holder.get("type") in _PUBLIC_HOLDER_TYPES for holder in holders):
        return ExternalAccess.public()

    users = _UserEmails(source_operations)
    emails: set[str] = set()
    groups: set[str] = set()
    for holder in holders:
        holder_type = holder.get("type")
        if holder_type == HOLDER_TYPE_USER:
            if email := users.email(_holder_user(holder)):
                emails.add(email)
        elif holder_type == HOLDER_TYPE_GROUP:
            if name := _holder_group_name(holder):
                groups.add(name)
        elif holder_type == HOLDER_TYPE_PROJECT_ROLE:
            role_id = _role_id(holder)
            if role_id is None:
                continue
            role = source_operations.get_project_role(
                project_key=project_key, role_id=role_id
            )
            for actor in role.get("actors") or []:
                if not isinstance(actor, dict):
                    continue
                if group_name := _role_actor_group(actor):
                    groups.add(group_name)
                elif (user := _role_actor_user(actor)) and (email := users.email(user)):
                    emails.add(email)
        else:
            logger.debug(
                "Jira project %s: Browse Projects holder %s adds no one",
                project_key,
                holder_type,
            )

    if add_prefix:
        groups = {
            build_ext_group_name_for_onyx(name, DocumentSource.JIRA) for name in groups
        }
    return ExternalAccess(
        external_user_emails=emails,
        external_user_group_ids=groups,
        is_public=False,
    )


def get_project_permissions(
    source_operations: JiraSourceOperations,
    jira_project: str,
    add_prefix: bool = False,
) -> ExternalAccess | None:
    try:
        scheme = source_operations.get_project_permission_scheme(
            project_key=jira_project
        )
        return map_project_permissions(
            source_operations, jira_project, scheme, add_prefix=add_prefix
        )
    except JiraApiError as e:
        logger.warning(
            "Cannot read the permissions of Jira project %s; its issues keep "
            "their last synced access: %s",
            jira_project,
            e,
        )
        return None
