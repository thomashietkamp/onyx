"""Who may read each Confluence space, from its view permissions. Content
without a read restriction gets its space's access. The connector reaches
this through ``onyx/connectors/confluence/access.py``.

Cloud returns the space's permissions with the space (``expand=permissions``):
the ``read`` operation on the ``space`` target lists the users and groups that
can view it, and ``anonymousAccess`` marks a space open to anonymous users.
Data Center reads them through the 9.1+ REST API, or JSON-RPC before 9.1
(``read_dc_space_subjects``). Reading space permissions needs a space admin
or a Confluence admin.

A space whose permissions cannot be read is left out of the result, so its
content gets no access from this sync and keeps its last synced ACL.
"""

from typing import Any

from requests import HTTPError

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.confluence.source_operations import (
    SPACE_PERMISSION_OPERATION_READ,
    SPACE_PERMISSION_TARGET_TYPE_SPACE,
    ConfluenceRetriesExhaustedError,
    ConfluenceSourceOperations,
    ConfluenceSpaceNotFoundError,
    ConfluenceSpacePermissionsVariant,
    has_anonymous_space_read,
    read_dc_space_subjects,
)
from onyx.connectors.exceptions import ConnectorValidationError
from onyx.external_permissions.confluence.users import confluence_user_email
from onyx.utils.logger import setup_logger

logger = setup_logger()

_SPACE_LIST_PAGE_SIZE = 50


def _is_space_read(permission: dict[str, Any]) -> bool:
    operation = permission.get("operation") or {}
    return (
        operation.get("operation") == SPACE_PERMISSION_OPERATION_READ
        and operation.get("targetType") == SPACE_PERMISSION_TARGET_TYPE_SPACE
    )


def _cloud_results(permission: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    subjects = permission.get("subjects") or {}
    results = (subjects.get(kind) or {}).get("results") or []
    return [entry for entry in results if isinstance(entry, dict)]


def map_cloud_space_permissions(
    source_operations: ConfluenceSourceOperations,
    permissions: list[dict[str, Any]],
) -> tuple[set[str], set[str], bool]:
    """Emails, group names, and whether anonymous users can view, from the
    Cloud ``space.permissions`` list."""
    emails: set[str] = set()
    groups: set[str] = set()
    is_public = False
    for permission in permissions:
        if not _is_space_read(permission):
            continue
        if permission.get("anonymousAccess"):
            is_public = True
        for user in _cloud_results(permission, "user"):
            email = confluence_user_email(source_operations, user, is_cloud=True)
            if email:
                emails.add(email)
        for group in _cloud_results(permission, "group"):
            name = group.get("name")
            if isinstance(name, str) and name:
                groups.add(name)
    return emails, groups, is_public


def _read_cloud_space(
    source_operations: ConfluenceSourceOperations, space_key: str
) -> tuple[set[str], set[str], bool]:
    permissions = source_operations.get_space_permissions(
        variant=ConfluenceSpacePermissionsVariant.CLOUD, space_key=space_key
    )
    return map_cloud_space_permissions(source_operations, permissions)


def _read_dc_space(
    source_operations: ConfluenceSourceOperations, space_key: str
) -> tuple[set[str], set[str], bool]:
    subjects = read_dc_space_subjects(source_operations, space_key)
    emails: set[str] = set()
    for user in subjects.users:
        email = source_operations.get_user_email(
            variant=subjects.user_email_variant(), user=user
        )
        if email:
            emails.add(email.lower())
    if subjects.api == ConfluenceSpacePermissionsVariant.DC_REST:
        is_public = has_anonymous_space_read(
            source_operations.get_anonymous_space_permissions(space_key=space_key)
        )
    else:
        is_public = subjects.has_anonymous_entry
    return emails, set(subjects.groups), is_public


def get_space_access(
    source_operations: ConfluenceSourceOperations,
    space_key: str,
    is_cloud: bool,
    add_prefix: bool = False,
) -> ExternalAccess:
    """The access of one space. Raises when its permissions cannot be read."""
    emails, groups, is_public = (
        _read_cloud_space(source_operations, space_key)
        if is_cloud
        else _read_dc_space(source_operations, space_key)
    )
    if add_prefix:
        groups = {
            build_ext_group_name_for_onyx(group, DocumentSource.CONFLUENCE)
            for group in groups
        }
    return ExternalAccess(
        external_user_emails=emails,
        external_user_group_ids=groups,
        is_public=is_public,
    )


def get_all_space_permissions(
    source_operations: ConfluenceSourceOperations,
    is_cloud: bool,
    add_prefix: bool = False,
) -> dict[str, ExternalAccess]:
    """The access of every visible space, by space key."""
    result: dict[str, ExternalAccess] = {}
    for space in source_operations.list_spaces(limit=_SPACE_LIST_PAGE_SIZE):
        space_key = space.get("key")
        if not isinstance(space_key, str) or not space_key:
            continue
        try:
            result[space_key] = get_space_access(
                source_operations, space_key, is_cloud, add_prefix
            )
        except (
            ConfluenceSpaceNotFoundError,
            ConfluenceRetriesExhaustedError,
            # Includes InsufficientPermissionsError (DC 500, CONFSERVER-99908).
            ConnectorValidationError,
            HTTPError,
        ) as e:
            logger.warning(
                "Cannot read the permissions of Confluence space %s; its content "
                "keeps its last synced access: %s",
                space_key,
                e,
            )
    return result
