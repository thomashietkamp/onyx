"""Who may read a OneDrive item, built from its Graph permissions. The OneDrive
connector reaches this through ``onyx/connectors/onedrive/access.py``.

The owner always reads their own file. A user grant adds the user's email; a
group grant adds the Entra group id, whose members the group sync supplies. A
grant with no read role (for example a pending invitation) adds nothing. An
anonymous link makes the file public, and so does an organization link when
``treat_organization_link_as_public`` is set. A user or group link adds the
people it names.
"""

from onyx.access.models import ExternalAccess
from onyx.connectors.onedrive.access import prefix_onedrive_external_groups
from onyx.connectors.onedrive.models import (
    GraphIdentity,
    GraphLinkScope,
    GraphSharePointIdentitySet,
    OneDrivePermission,
)

_READ_ROLES = frozenset({"read", "write", "owner"})


def _identity_email(identity: GraphIdentity | None) -> str | None:
    if identity is None:
        return None
    email = identity.email or identity.user_principal_name
    if email and "@" in email:
        return email.lower()
    if identity.login_name and "@" in identity.login_name:
        return identity.login_name.rsplit("|", 1)[-1].lower()
    return None


def _add_identity_set(
    identity_set: GraphSharePointIdentitySet | None,
    emails: set[str],
    group_ids: set[str],
) -> None:
    if identity_set is None:
        return
    if identity_set.group is not None and identity_set.group.id:
        group_ids.add(identity_set.group.id.lower())
    for identity in (identity_set.user, identity_set.site_user):
        if email := _identity_email(identity):
            emails.add(email)


def map_onedrive_permissions(
    permissions: list[OneDrivePermission],
    owner_email: str,
    treat_organization_link_as_public: bool,
    add_prefix: bool,
) -> ExternalAccess:
    emails: set[str] = {owner_email.lower()} if owner_email else set()
    group_ids: set[str] = set()
    is_public = False
    for permission in permissions:
        if permission.roles and not set(permission.roles) & _READ_ROLES:
            continue
        link = permission.link
        if link is not None and link.scope is not None:
            if link.scope == GraphLinkScope.ANONYMOUS or (
                link.scope == GraphLinkScope.ORGANIZATION
                and treat_organization_link_as_public
            ):
                is_public = True
                continue
        _add_identity_set(permission.granted_to_v2, emails, group_ids)
        for identity_set in permission.granted_to_identities_v2:
            _add_identity_set(identity_set, emails, group_ids)
    access = ExternalAccess(
        external_user_emails=emails,
        external_user_group_ids=group_ids,
        is_public=is_public,
    )
    return prefix_onedrive_external_groups(access) if add_prefix else access
