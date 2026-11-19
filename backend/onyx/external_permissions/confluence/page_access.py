"""Who may read a Confluence page or attachment, from its read restrictions.
The connector reaches these functions by name through
``onyx/connectors/confluence/access.py``.

A read restriction on a page applies to its descendants too, so a reader must
pass the restriction of the page and of every restricted ancestor. Each
restriction allows some users and some groups. Group members are known only
at query time, so the intersection of several restrictions is kept only where
it is certain: a user listed in every restriction, and a group listed in
every restriction. A reader who passes one restriction only through a group
can lose access in Onyx; no one gains access that Confluence denies.

None means "no read restriction": the space permissions apply.
"""

from typing import Any

from pydantic import BaseModel

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.confluence.source_operations import ConfluenceSourceOperations
from onyx.external_permissions.confluence.users import (
    confluence_user_email,
    is_cloud_gateway,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()


class ReadRestriction(BaseModel):
    """The users (emails) and groups (names) that one restriction allows."""

    user_emails: set[str]
    group_names: set[str]


def _results(restrictions: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    read = restrictions.get("read") or {}
    by_kind = (read.get("restrictions") or {}).get(kind) or {}
    results = by_kind.get("results") or []
    return [entry for entry in results if isinstance(entry, dict)]


def parse_read_restriction(
    source_operations: ConfluenceSourceOperations,
    restrictions: dict[str, Any] | None,
    *,
    is_cloud: bool,
) -> ReadRestriction | None:
    """The read restriction of a ``restrictions`` (or
    ``restriction/byOperation``) object, or None when it has none."""
    if not restrictions:
        return None
    users = _results(restrictions, "user")
    groups = _results(restrictions, "group")
    if not users and not groups:
        return None
    emails: set[str] = set()
    for user in users:
        email = confluence_user_email(source_operations, user, is_cloud=is_cloud)
        if email:
            emails.add(email)
        else:
            logger.debug(
                "Confluence user %s in a read restriction has no visible email",
                user.get("accountId") or user.get("userKey") or user.get("username"),
            )
    group_names = {
        name for group in groups if isinstance(name := group.get("name"), str) and name
    }
    return ReadRestriction(user_emails=emails, group_names=group_names)


def intersect_restrictions(restrictions: list[ReadRestriction]) -> ReadRestriction:
    """What surely passes every restriction (see the module docstring)."""
    if not restrictions:
        raise ValueError("intersect_restrictions needs at least one restriction")
    if len(restrictions) == 1:
        return restrictions[0]
    groups = set.intersection(*(r.group_names for r in restrictions))
    emails = set.intersection(*(r.user_emails for r in restrictions))
    return ReadRestriction(user_emails=emails, group_names=groups)


def _to_access(restriction: ReadRestriction, add_prefix: bool) -> ExternalAccess:
    group_ids = (
        {
            build_ext_group_name_for_onyx(name, DocumentSource.CONFLUENCE)
            for name in restriction.group_names
        }
        if add_prefix
        else set(restriction.group_names)
    )
    return ExternalAccess(
        external_user_emails=restriction.user_emails,
        external_user_group_ids=group_ids,
        is_public=False,
    )


def _resolve(
    page_id: str,
    restrictions: list[ReadRestriction | None],
    add_prefix: bool,
) -> ExternalAccess | None:
    present = [r for r in restrictions if r is not None]
    if not present:
        return None
    access = _to_access(intersect_restrictions(present), add_prefix)
    if not access.external_user_emails and not access.external_user_group_ids:
        logger.info(
            "Confluence content %s is restricted to no one Onyx can map", page_id
        )
    return access


def get_page_restrictions(
    source_operations: ConfluenceSourceOperations,
    page_id: str,
    page_restrictions: dict[str, Any],
    ancestors: list[dict[str, Any]],
    add_prefix: bool = False,
) -> ExternalAccess | None:
    """The access of content whose ancestors carry their restrictions inline
    (``ancestors.restrictions.read.restrictions.*`` expanded)."""
    is_cloud = is_cloud_gateway(source_operations)
    restrictions = [
        parse_read_restriction(source_operations, page_restrictions, is_cloud=is_cloud)
    ] + [
        parse_read_restriction(
            source_operations, ancestor.get("restrictions"), is_cloud=is_cloud
        )
        for ancestor in ancestors
    ]
    return _resolve(page_id, restrictions, add_prefix)


def get_page_restrictions_with_per_ancestor_fetch(
    source_operations: ConfluenceSourceOperations,
    page_id: str,
    page_restrictions: dict[str, Any],
    ancestors: list[dict[str, Any]],
    ancestor_restrictions_cache: dict[str, dict[str, Any] | None],
    add_prefix: bool = False,
) -> ExternalAccess | None:
    """``get_page_restrictions`` for ancestors listed without restrictions
    (CONFCLOUD-77618): each ancestor's restrictions are read with
    ``restriction/byOperation`` once per sync, through
    ``ancestor_restrictions_cache``. An ancestor that cannot be read (for
    example another user's draft) adds no restriction. An ancestor that
    carries ``restrictions`` inline is not fetched."""
    is_cloud = is_cloud_gateway(source_operations)
    restrictions = [
        parse_read_restriction(source_operations, page_restrictions, is_cloud=is_cloud)
    ]
    for ancestor in ancestors:
        if "restrictions" in ancestor:
            raw = ancestor.get("restrictions")
        else:
            raw_id = ancestor.get("id")
            if raw_id is None or raw_id == "":
                continue
            ancestor_id = str(raw_id)
            if ancestor_id not in ancestor_restrictions_cache:
                ancestor_restrictions_cache[ancestor_id] = (
                    source_operations.get_content_read_restrictions(
                        content_id=ancestor_id
                    )
                )
            raw = ancestor_restrictions_cache[ancestor_id]
        restrictions.append(
            parse_read_restriction(source_operations, raw, is_cloud=is_cloud)
        )
    return _resolve(page_id, restrictions, add_prefix)
