"""Who may read an Egnyte folder, built from its effective grants. The Egnyte
connector reaches this through ``onyx/connectors/egnyte/access.py``.

A folder grant names a user by username or a group by display name. A user
grant adds the active user's email. A group grant adds the group id, scoped by
the domain, whose members the group sync supplies. The default groups (all
power users, all standard users) are not listed with the custom groups, so the
group sync derives their members from the user types. A grant of "None" adds
nothing. A folder without listed grants (Egnyte lists none for "/" and
"/Private") is readable by nobody.

Group ids are bare: the connector and the doc sync add the source prefix.
"""

from onyx.access.models import ExternalAccess
from onyx.connectors.egnyte.access import EgnyteUnknownGroupError
from onyx.connectors.egnyte.models import (
    EgnyteDirectory,
    EgnytePermissionGrant,
    EgnytePermissions,
    EgnyteUser,
    EgnyteUserType,
)
from onyx.connectors.egnyte.source_operations import EgnyteSourceOperations

_NO_ACCESS = "none"

ALL_POWER_USERS_GROUP_ID = "default-all-power-users"
ALL_STANDARD_USERS_GROUP_ID = "default-all-standard-users"
ALL_USERS_GROUP_ID = "default-all-users"

# Display name (lowercased) of each default group to its id.
DEFAULT_GROUP_IDS_BY_NAME: dict[str, str] = {
    "all power users": ALL_POWER_USERS_GROUP_ID,
    "all standard users": ALL_STANDARD_USERS_GROUP_ID,
    "all users": ALL_USERS_GROUP_ID,
}

# The user types in each default group. Administrators are power users.
DEFAULT_GROUP_USER_TYPES: dict[str, frozenset[EgnyteUserType]] = {
    ALL_POWER_USERS_GROUP_ID: frozenset({EgnyteUserType.ADMIN, EgnyteUserType.POWER}),
    ALL_STANDARD_USERS_GROUP_ID: frozenset({EgnyteUserType.STANDARD}),
    ALL_USERS_GROUP_ID: frozenset(EgnyteUserType),
}


def normalize_username(username: str) -> str:
    """Egnyte usernames are case-insensitive."""
    return username.strip().lower()


def domain_group_id(domain: str, group_id: str) -> str:
    """Two Egnyte domains on one tenant must not share group ids, because a
    user's external groups are read across every pair."""
    return f"{domain.strip().lower()}:{group_id}"


def active_user_emails(users: list[EgnyteUser]) -> dict[str, str]:
    return {
        normalize_username(user.user_name): user.email.strip().lower()
        for user in users
        if user.active and user.email.strip()
    }


def build_directory(ops: EgnyteSourceOperations) -> EgnyteDirectory:
    group_ids_by_name: dict[str, str] = dict(DEFAULT_GROUP_IDS_BY_NAME)
    for group in ops.list_groups():
        group_ids_by_name[group.display_name.strip().lower()] = group.id
    return EgnyteDirectory(
        emails_by_username=active_user_emails(ops.list_users()),
        group_ids_by_name=group_ids_by_name,
    )


def _grants_read(grant: EgnytePermissionGrant) -> bool:
    return grant.permission.strip().lower() != _NO_ACCESS


def folder_access(
    domain: str,
    permissions: EgnytePermissions | None,
    directory: EgnyteDirectory,
) -> ExternalAccess:
    if permissions is None:
        return ExternalAccess.empty()
    emails: set[str] = set()
    for grant in permissions.users:
        if not _grants_read(grant):
            continue
        # An inactive or deleted user reads nothing.
        email = directory.emails_by_username.get(normalize_username(grant.subject))
        if email:
            emails.add(email)
    group_ids: set[str] = set()
    for grant in permissions.groups:
        if not _grants_read(grant):
            continue
        group_id = directory.group_ids_by_name.get(grant.subject.strip().lower())
        if group_id is None:
            raise EgnyteUnknownGroupError(
                f"Egnyte folder grant names group '{grant.subject}', which is "
                "neither a custom group nor a default one"
            )
        group_ids.add(domain_group_id(domain, group_id))
    return ExternalAccess(
        external_user_emails=emails,
        external_user_group_ids=group_ids,
        is_public=False,
    )
