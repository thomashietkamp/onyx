from collections.abc import Callable
from typing import Any, cast

from onyx.access.models import ExternalAccess
from onyx.connectors.confluence.source_operations import ConfluenceSourceOperations
from onyx.utils.variable_functionality import fetch_versioned_implementation


def get_page_restrictions(
    source_operations: ConfluenceSourceOperations,
    page_id: str,
    page_restrictions: dict[str, Any],
    ancestors: list[dict[str, Any]],
    add_prefix: bool = False,
) -> ExternalAccess | None:
    """
    Get page access restrictions for a Confluence page
    (``onyx/external_permissions/confluence/page_access.py``).

    add_prefix: True for the indexing path (Document.external_access) so group
    ids carry the source-type prefix the search filter expects. False for the
    permission-sync path, where upsert_document_external_perms adds the prefix.

    Returns ExternalAccess for the page, or None if no read restriction
    applies (the space permissions apply then).
    """
    get_all_page_restrictions_impl = cast(
        Callable[
            [
                ConfluenceSourceOperations,
                str,
                dict[str, Any],
                list[dict[str, Any]],
                bool,
            ],
            ExternalAccess | None,
        ],
        fetch_versioned_implementation(
            "onyx.external_permissions.confluence.page_access", "get_page_restrictions"
        ),
    )

    return get_all_page_restrictions_impl(
        source_operations, page_id, page_restrictions, ancestors, add_prefix
    )


def get_page_restrictions_with_per_ancestor_fetch(
    source_operations: ConfluenceSourceOperations,
    page_id: str,
    page_restrictions: dict[str, Any],
    ancestors: list[dict[str, Any]],
    ancestor_restrictions_cache: dict[str, dict[str, Any] | None],
    add_prefix: bool = False,
) -> ExternalAccess | None:
    """CONFCLOUD-77618 variant of `get_page_restrictions`. Ancestors
    arrive without inline restrictions; each is fetched via
    `restriction/byOperation` with 403/404 swallowed for drafts."""
    get_per_ancestor_impl = cast(
        Callable[
            [
                ConfluenceSourceOperations,
                str,
                dict[str, Any],
                list[dict[str, Any]],
                dict[str, dict[str, Any] | None],
                bool,
            ],
            ExternalAccess | None,
        ],
        fetch_versioned_implementation(
            "onyx.external_permissions.confluence.page_access",
            "get_page_restrictions_with_per_ancestor_fetch",
        ),
    )

    return get_per_ancestor_impl(
        source_operations,
        page_id,
        page_restrictions,
        ancestors,
        ancestor_restrictions_cache,
        add_prefix,
    )


def get_all_space_permissions(
    source_operations: ConfluenceSourceOperations,
    is_cloud: bool,
    add_prefix: bool = False,
) -> dict[str, ExternalAccess]:
    """
    Get access permissions for all spaces in Confluence
    (``onyx/external_permissions/confluence/space_access.py``).

    add_prefix: True for the indexing path (Document.external_access) so group
    ids carry the source-type prefix the search filter expects. False for the
    permission-sync path, where upsert_document_external_perms adds the prefix.

    Returns a mapping of space key to ExternalAccess. A space whose
    permissions cannot be read is left out.
    """
    get_all_space_permissions_impl = cast(
        Callable[
            [ConfluenceSourceOperations, bool, bool],
            dict[str, ExternalAccess],
        ],
        fetch_versioned_implementation(
            "onyx.external_permissions.confluence.space_access",
            "get_all_space_permissions",
        ),
    )

    return get_all_space_permissions_impl(source_operations, is_cloud, add_prefix)
