"""Who may read the documents of a GitLab project. The connector reaches this
through ``onyx/connectors/gitlab/access.py``.

A document kind is open to everyone the project's visibility admits only when
its feature is "enabled". Otherwise only project members read it:

- code files and merge requests: members with Reporter or higher. A merge
  request also needs the repository feature, because it shows code.
- issues: members with Guest or higher.
- confidential issues: members with Reporter or higher, plus the author and
  the assignees, whatever the project visibility.

Visibility "public" makes an open feature public. "internal" admits every
signed-in, non-external user of the instance, through the instance users
group. On gitlab.com that group cannot be listed, so an internal project there
gets its member groups only: Onyx never grants more than GitLab does. An
unknown visibility or feature level counts as private.

Group ids carry a hash of the instance URL, because project ids repeat across
GitLab instances and a user's groups are matched across all pairs. The ids
returned here carry the Onyx source prefix; the doc sync prefix is idempotent.
The group sync yields the same ids without the prefix.
"""

from onyx.access.models import ExternalAccess
from onyx.access.utils import (
    build_ext_group_name_for_onyx,
    build_instance_scoped_group_id,
)
from onyx.configs.constants import DocumentSource
from onyx.connectors.gitlab.models import (
    FEATURE_ENABLED,
    VISIBILITY_INTERNAL,
    VISIBILITY_PUBLIC,
    GitlabIssueReaders,
    GitlabProjectAccessInfo,
)

ROLE_REPORTER = "reporter"
ROLE_GUEST = "guest"


def project_role_group_id(instance_url: str, project_id: int, role: str) -> str:
    """Raw id of the project members with ``role`` or a higher role. The guest
    group holds every member of the reporter group."""
    return build_instance_scoped_group_id(instance_url, f"project_{project_id}_{role}")


def instance_users_group_id(instance_url: str) -> str:
    """Raw id of every active, non-external user of a self-managed instance."""
    return build_instance_scoped_group_id(instance_url, "instance_users")


def uses_instance_users_group(info: GitlabProjectAccessInfo) -> bool:
    return info.visibility == VISIBILITY_INTERNAL and not info.is_gitlab_com


def _access(
    group_ids: set[str], emails: frozenset[str] = frozenset()
) -> ExternalAccess:
    return ExternalAccess(
        external_user_emails=set(emails),
        external_user_group_ids={
            build_ext_group_name_for_onyx(group_id, DocumentSource.GITLAB)
            for group_id in group_ids
        },
        is_public=False,
    )


def _feature_access(
    info: GitlabProjectAccessInfo, feature_levels: list[str | None], member_role: str
) -> ExternalAccess:
    members = project_role_group_id(info.instance_url, info.project_id, member_role)
    if any(level != FEATURE_ENABLED for level in feature_levels):
        return _access({members})
    if info.visibility == VISIBILITY_PUBLIC:
        return ExternalAccess(
            external_user_emails=set(),
            external_user_group_ids=set(),
            is_public=True,
        )
    if uses_instance_users_group(info):
        return _access({members, instance_users_group_id(info.instance_url)})
    return _access({members})


def code_access(info: GitlabProjectAccessInfo) -> ExternalAccess:
    return _feature_access(info, [info.repository_access_level], ROLE_REPORTER)


def merge_request_access(info: GitlabProjectAccessInfo) -> ExternalAccess:
    return _feature_access(
        info,
        [info.repository_access_level, info.merge_requests_access_level],
        ROLE_REPORTER,
    )


def issue_access(
    info: GitlabProjectAccessInfo, issue: GitlabIssueReaders
) -> ExternalAccess:
    if issue.confidential:
        return _access(
            {project_role_group_id(info.instance_url, info.project_id, ROLE_REPORTER)},
            issue.author_and_assignee_emails,
        )
    return _feature_access(info, [info.issues_access_level], ROLE_GUEST)
