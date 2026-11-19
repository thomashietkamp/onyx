"""Who may read a SharePoint item, page, library or folder, built from its role
assignments. The SharePoint connector (and Teams, for channel files) reaches
these functions through ``onyx/connectors/microsoft_utils/sharepoint_access.py``.

Rules:
- Only assignments that grant more than Limited Access count.
- A user principal adds their email.
- An Entra group adds its id; the group sync supplies its members.
- A SharePoint site group is expanded into its users and Entra groups, once
  per site and group (``SharepointPermissionCache``).
- An "everyone" claim makes the object public. With
  ``treat_sharing_link_as_public``, an organization or anonymous sharing link
  on a file does too.
"""

import re
from typing import Any

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.microsoft_utils.drive_items import DriveItemData
from onyx.connectors.microsoft_utils.models import (
    SharepointPrincipal,
    SharepointSecurable,
    SharepointSecurableKind,
)
from onyx.connectors.microsoft_utils.sharepoint_access import (
    SharepointGroup,
    SharepointGroupExpansion,
    SharepointPermissionCache,
)
from onyx.connectors.microsoft_utils.sharepoint_principals import (
    SharepointPrincipalType,
    granting_members,
    is_public_login_name,
    resolve_group_id,
)
from onyx.connectors.microsoft_utils.sharepoint_rest import SharepointPermissionReader
from onyx.db.enums import HierarchyNodeType
from onyx.utils.logger import setup_logger

logger = setup_logger()

_PUBLIC_LINK_SCOPES = frozenset({"organization", "anonymous"})
_EMAIL_RE = re.compile(r"[^|\s]+@[^|\s]+")


class _Access:
    def __init__(self) -> None:
        self.emails: set[str] = set()
        self.group_ids: set[str] = set()
        self.is_public = False

    def build(self, add_prefix: bool) -> ExternalAccess:
        group_ids = (
            {
                build_ext_group_name_for_onyx(g, DocumentSource.SHAREPOINT)
                for g in self.group_ids
            }
            if add_prefix
            else set(self.group_ids)
        )
        return ExternalAccess(
            external_user_emails=self.emails,
            external_user_group_ids=group_ids,
            is_public=self.is_public,
        )


def _user_email(principal: SharepointPrincipal) -> str | None:
    if principal.user_principal_name and "@" in principal.user_principal_name:
        return principal.user_principal_name.lower()
    match = _EMAIL_RE.search(principal.login_name)
    return match.group(0).lower() if match else None


def _site_group_members(
    reader: SharepointPermissionReader,
    site_url: str,
    group_name: str,
    cache: SharepointPermissionCache,
) -> set[SharepointGroup]:
    key = f"{site_url}|{group_name}"
    cached = cache.group_expansions.get(key)
    if cached is None:
        members = reader.list_site_group_users(site_url=site_url, group_name=group_name)
        cached = SharepointGroupExpansion(
            nested_groups={
                SharepointGroup(
                    name=member.user_principal_name or member.title,
                    login_name=member.login_name,
                    principal_type=member.principal_type,
                )
                for member in members
            }
        )
        cache.group_expansions[key] = cached
    return cached.nested_groups


def _add_principal(
    access: _Access,
    reader: SharepointPermissionReader,
    principal: SharepointPrincipal,
    site_url: str,
    cache: SharepointPermissionCache,
) -> None:
    if is_public_login_name(principal.login_name):
        access.is_public = True
        return
    if principal.principal_type == SharepointPrincipalType.USER:
        if email := _user_email(principal):
            access.emails.add(email)
        return
    if principal.principal_type == SharepointPrincipalType.ENTRA_GROUP:
        if group_id := resolve_group_id(
            reader, principal.login_name or principal.title
        ):
            access.group_ids.add(group_id.lower())
        return
    if principal.principal_type == SharepointPrincipalType.SHAREPOINT_GROUP:
        for member in _site_group_members(reader, site_url, principal.title, cache):
            _add_principal(
                access,
                reader,
                SharepointPrincipal(
                    principal_type=member.principal_type,
                    login_name=member.login_name,
                    title=member.name,
                    user_principal_name=member.name if "@" in member.name else None,
                ),
                site_url,
                cache,
            )


def _securable_access(
    reader: SharepointPermissionReader,
    site_url: str,
    securable: SharepointSecurable,
    cache: SharepointPermissionCache,
) -> _Access:
    access = _Access()
    assignments = reader.list_role_assignments(site_url=site_url, securable=securable)
    for principal in granting_members(assignments):
        _add_principal(access, reader, principal, site_url, cache)
    return access


def get_external_access_from_sharepoint(
    reader: SharepointPermissionReader,
    site_url: str,
    list_id: str | None,
    drive_item: DriveItemData | None,
    site_page: dict[str, Any] | None,
    add_prefix: bool,
    treat_sharing_link_as_public: bool,
    permission_cache: SharepointPermissionCache,
) -> ExternalAccess:
    if drive_item is not None:
        item_id = reader.get_list_item_id(item=drive_item)
        securable = (
            SharepointSecurable(
                kind=SharepointSecurableKind.LIST_ITEM, list_id=list_id, item_id=item_id
            )
            if item_id is not None
            else SharepointSecurable(
                kind=SharepointSecurableKind.LIBRARY, list_id=list_id
            )
        )
    elif site_page is not None:
        securable = SharepointSecurable(
            kind=SharepointSecurableKind.PAGE, page_url=site_page.get("webUrl")
        )
    else:
        securable = SharepointSecurable(kind=SharepointSecurableKind.SITE)

    access = _securable_access(reader, site_url, securable, permission_cache)
    if drive_item is not None and treat_sharing_link_as_public and not access.is_public:
        scopes = reader.list_sharing_link_scopes(item=drive_item)
        access.is_public = any(scope in _PUBLIC_LINK_SCOPES for scope in scopes)
    return access.build(add_prefix)


def get_hierarchy_node_external_access_from_sharepoint(
    reader: SharepointPermissionReader,
    site_url: str,
    node_type: HierarchyNodeType,
    list_id: str | None,
    folder_server_relative_path: str | None,
    permission_cache: SharepointPermissionCache,
) -> ExternalAccess:
    """Hierarchy nodes store their access with the source prefix, as the
    hierarchy filter compares them with the user's stored group ids."""
    if node_type == HierarchyNodeType.FOLDER and folder_server_relative_path:
        securable = SharepointSecurable(
            kind=SharepointSecurableKind.FOLDER,
            folder_unique_id=reader.get_folder_unique_id(
                site_url=site_url, server_relative_path=folder_server_relative_path
            ),
        )
    elif node_type == HierarchyNodeType.DRIVE and list_id:
        securable = SharepointSecurable(
            kind=SharepointSecurableKind.LIBRARY, list_id=list_id
        )
    else:
        securable = SharepointSecurable(kind=SharepointSecurableKind.SITE)
    return _securable_access(reader, site_url, securable, permission_cache).build(
        add_prefix=True
    )
