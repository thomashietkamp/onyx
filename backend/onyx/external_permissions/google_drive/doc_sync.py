"""Who may read a Google Drive file or folder, built from its Drive permissions.
The connector reaches these functions by name through
``fetch_versioned_implementation`` (``connectors/google_drive/doc_conversion.py``
and ``connectors/google_drive/file_retrieval.py``).

A ``user`` permission adds the user's email. A ``group`` permission adds the
Google group's email as a group id, whose members the group sync supplies. A
``domain`` permission adds ``build_domain_group_id(domain)``; the group sync
fills it with the Workspace roster. An ``anyone`` permission makes the file
public. Every role (reader, commenter, writer, organizer, owner) can read.

A file listing returns ``permissions`` only for files the listing user can
share, and never for shared drive items, so the permissions are otherwise read
with ``permissions.list`` (which also returns the inherited shared drive
members).
"""

from collections.abc import Iterable
from typing import Any

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_domain_group_id, build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.google_drive.file_retrieval import PERMISSION_FULL_DESCRIPTION
from onyx.connectors.google_drive.source_operations import (
    GoogleDriveHttpError,
    GoogleDriveRefreshError,
    GoogleDriveSourceOperations,
)
from onyx.connectors.google_utils.shared_constants import GoogleDriveFileType
from onyx.utils.logger import setup_logger

logger = setup_logger()

_PERMISSION_LIST_FIELDS = f"nextPageToken, {PERMISSION_FULL_DESCRIPTION}"
# The statuses that mean "this user cannot read the permissions"; another user
# may still read them.
_UNREADABLE_STATUSES = frozenset({401, 403, 404})


def map_drive_permissions(
    permissions: Iterable[dict[str, Any]], *, add_prefix: bool
) -> ExternalAccess:
    """Maps raw Drive permission resources to an ``ExternalAccess``.
    ``add_prefix`` namespaces the group ids (the indexing path); the doc sync
    write path adds the prefix itself."""
    emails: set[str] = set()
    group_ids: set[str] = set()
    is_public = False
    for permission in permissions:
        if permission.get("deleted"):
            continue
        permission_type = permission.get("type")
        email = permission.get("emailAddress")
        if permission_type == "user":
            if isinstance(email, str) and email:
                emails.add(email.lower())
        elif permission_type == "group":
            if isinstance(email, str) and email:
                group_ids.add(email.lower())
        elif permission_type == "domain":
            domain = permission.get("domain")
            if isinstance(domain, str) and domain:
                group_ids.add(build_domain_group_id(domain))
        elif permission_type == "anyone":
            is_public = True
        else:
            logger.debug("Skipping Drive permission of type %s", permission_type)
    if add_prefix:
        group_ids = {
            build_ext_group_name_for_onyx(group_id, DocumentSource.GOOGLE_DRIVE)
            for group_id in group_ids
        }
    return ExternalAccess(
        external_user_emails=emails,
        external_user_group_ids=group_ids,
        is_public=is_public,
    )


def _inline_permissions(item: GoogleDriveFileType) -> list[dict[str, Any]] | None:
    """The permissions the listing returned, or None when it returned none.
    An empty list is not an answer: a file always has an owner or members."""
    permissions = item.get("permissions")
    if isinstance(permissions, list) and permissions:
        return permissions
    return None


def _read_permissions(
    ops: GoogleDriveSourceOperations,
    file_id: str,
    readers: Iterable[str | None],
) -> list[dict[str, Any]] | None:
    """The permissions of a file, read as the first of ``readers`` that can
    read them. None when no reader can. Errors other than "cannot read" (for
    example a server error) are raised, so a sync does not narrow an ACL on a
    transient failure."""
    tried: set[str] = set()
    for reader in readers:
        if not reader or reader in tried:
            continue
        tried.add(reader)
        try:
            return list(
                ops.list_file_permissions(
                    user_email=reader, file_id=file_id, fields=_PERMISSION_LIST_FIELDS
                )
            )
        except GoogleDriveRefreshError as e:
            # The user is outside the domain or cannot be impersonated.
            logger.debug("Cannot impersonate %s to read %s: %s", reader, file_id, e)
        except GoogleDriveHttpError as e:
            if e.status_code not in _UNREADABLE_STATUSES or e.rate_limited:
                raise
            logger.debug(
                "%s cannot read the permissions of %s (HTTP %s)",
                reader,
                file_id,
                e.status_code,
            )
    return None


def get_external_access_for_raw_gdrive_file(
    file: GoogleDriveFileType,
    company_domain: str,  # noqa: ARG001
    ops: GoogleDriveSourceOperations,
    retriever_email: str | None,
    admin_email: str,
    fallback_user_email: str,
    add_prefix: bool = False,
    fallback_retriever_email: str | None = None,
) -> ExternalAccess:
    """The access of a file. Reads the permissions as ``retriever_email``, then
    ``fallback_retriever_email``, then ``admin_email``. When none can read them
    (for example a file owned outside the domain), only
    ``fallback_user_email``, who listed the file, gets access."""
    permissions = _inline_permissions(file)
    file_id = file.get("id")
    if permissions is None and isinstance(file_id, str):
        permissions = _read_permissions(
            ops, file_id, (retriever_email, fallback_retriever_email, admin_email)
        )
    if permissions is None:
        logger.info(
            "No user can read the permissions of Drive file %s; giving access to "
            "%s only",
            file_id,
            fallback_user_email,
        )
        return ExternalAccess(
            external_user_emails={fallback_user_email.lower()},
            external_user_group_ids=set(),
            is_public=False,
        )
    return map_drive_permissions(permissions, add_prefix=add_prefix)


def get_external_access_for_folder(
    folder: GoogleDriveFileType,
    google_domain: str,  # noqa: ARG001
    ops: GoogleDriveSourceOperations,
    user_email: str,
    add_prefix: bool = False,
) -> ExternalAccess:
    """The access of a folder's hierarchy node, read as ``user_email`` when the
    folder metadata has no permissions. A folder whose permissions no one can
    read is private."""
    permissions = _inline_permissions(folder)
    folder_id = folder.get("id")
    if permissions is None and isinstance(folder_id, str):
        permissions = _read_permissions(ops, folder_id, (user_email,))
    if permissions is None:
        return ExternalAccess.empty()
    return map_drive_permissions(permissions, add_prefix=add_prefix)
