"""
Permissioning / AccessControl logic for JIRA Projects + Issues.
"""

from collections.abc import Callable
from typing import cast

from onyx.access.models import ExternalAccess
from onyx.connectors.jira.source_operations import JiraSourceOperations
from onyx.utils.variable_functionality import fetch_versioned_implementation


def get_project_permissions(
    source_operations: JiraSourceOperations,
    jira_project: str,
    add_prefix: bool = False,
) -> ExternalAccess | None:
    """
    Fetch the project + issue level permissions / access-control
    (``onyx/external_permissions/jira/page_access.py``).

    Args:
        source_operations: The Jira gateway.
        jira_project: The JIRA project string.
        add_prefix: When True, prefix group IDs with source type (for indexing path).
                   When False (default), leave unprefixed (for permission sync path
                   where upsert_document_external_perms handles prefixing).

    Returns:
        ExternalAccess for the project's issues. None if the project's permissions
        cannot be read.
    """

    get_project_permissions_impl = cast(
        Callable[
            [JiraSourceOperations, str, bool],
            ExternalAccess | None,
        ],
        fetch_versioned_implementation(
            "onyx.external_permissions.jira.page_access", "get_project_permissions"
        ),
    )

    return get_project_permissions_impl(
        source_operations,
        jira_project,
        add_prefix,
    )
