from collections.abc import Callable
from typing import cast

from onyx.access.models import ExternalAccess
from onyx.connectors.youtrack.models import YouTrackIssueAccess
from onyx.utils.variable_functionality import fetch_versioned_implementation

_ACCESS_MODULE: str = "onyx.external_permissions.youtrack.access"


def get_issue_access(base_url: str, issue: YouTrackIssueAccess) -> ExternalAccess:
    issue_access = cast(
        Callable[[str, YouTrackIssueAccess], ExternalAccess],
        fetch_versioned_implementation(_ACCESS_MODULE, "issue_access"),
    )
    return issue_access(base_url, issue)


def get_project_node_access(base_url: str, project_id: str) -> ExternalAccess:
    project_node_access = cast(
        Callable[[str, str], ExternalAccess],
        fetch_versioned_implementation(_ACCESS_MODULE, "project_node_access"),
    )
    return project_node_access(base_url, project_id)
