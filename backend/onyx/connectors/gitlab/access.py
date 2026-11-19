from collections.abc import Callable
from typing import cast

from onyx.access.models import ExternalAccess
from onyx.connectors.gitlab.models import GitlabIssueReaders, GitlabProjectAccessInfo
from onyx.utils.variable_functionality import fetch_versioned_implementation

_ACCESS_MODULE: str = "onyx.external_permissions.gitlab.access"


def get_code_access(info: GitlabProjectAccessInfo) -> ExternalAccess:
    code_access = cast(
        Callable[[GitlabProjectAccessInfo], ExternalAccess],
        fetch_versioned_implementation(_ACCESS_MODULE, "code_access"),
    )
    return code_access(info)


def get_merge_request_access(info: GitlabProjectAccessInfo) -> ExternalAccess:
    merge_request_access = cast(
        Callable[[GitlabProjectAccessInfo], ExternalAccess],
        fetch_versioned_implementation(_ACCESS_MODULE, "merge_request_access"),
    )
    return merge_request_access(info)


def get_issue_access(
    info: GitlabProjectAccessInfo, issue: GitlabIssueReaders
) -> ExternalAccess:
    issue_access = cast(
        Callable[[GitlabProjectAccessInfo, GitlabIssueReaders], ExternalAccess],
        fetch_versioned_implementation(_ACCESS_MODULE, "issue_access"),
    )
    return issue_access(info, issue)
