"""Who may read a YouTrack issue or project, as external group ids.

The connector reaches this through ``onyx/connectors/youtrack/access.py``.
An issue's ACL names groups only. ``group_sync.py`` fills each group with the
emails of the users that YouTrack lets read issues of the project:

- the project group: every user with the Read Issue permission in the
  project. In a helpdesk project, customers are left out, because a customer
  reads only the tickets they reported.
- a project-and-group group: the members of a visibility group who can read
  the project. A restricted issue is visible to its permitted groups and users,
  and only to the ones that can read the project.
- a project-and-user group: one user, when that user can read the project.

Group ids carry a hash of the instance URL, because project, group and user
ids repeat across YouTrack instances. The ids returned here carry the Onyx
source prefix, because hierarchy node access is stored as given; the doc sync
prefix is idempotent. The group sync yields the same ids without the prefix.
"""

from onyx.access.models import ExternalAccess
from onyx.access.utils import (
    build_ext_group_name_for_onyx,
    build_instance_scoped_group_id,
)
from onyx.configs.constants import DocumentSource
from onyx.connectors.youtrack.models import YouTrackIssueAccess


def project_group_id(base_url: str, project_id: str) -> str:
    """Raw id of the group of users who can read the project's issues."""
    return build_instance_scoped_group_id(base_url, f"project:{project_id}")


def project_visibility_group_id(base_url: str, project_id: str, group_id: str) -> str:
    """Raw id of the members of a YouTrack group who can read the project."""
    return f"{project_group_id(base_url, project_id)}:group:{group_id}"


def project_user_group_id(base_url: str, project_id: str, user_id: str) -> str:
    """Raw id of a single user, when that user can read the project."""
    return f"{project_group_id(base_url, project_id)}:user:{user_id}"


def issue_group_ids(base_url: str, issue: YouTrackIssueAccess) -> set[str]:
    """The raw group ids whose members may read the issue."""
    project_id: str = issue.project.id
    group_ids: set[str] = set()
    visibility = issue.visibility
    if visibility is not None and visibility.is_restricted:
        group_ids.update(
            project_visibility_group_id(base_url, project_id, group.id)
            for group in visibility.permitted_groups
        )
        group_ids.update(
            project_user_group_id(base_url, project_id, user.id)
            for user in visibility.permitted_users
        )
    else:
        group_ids.add(project_group_id(base_url, project_id))
    if issue.project.is_helpdesk and issue.reporter is not None:
        group_ids.add(project_user_group_id(base_url, project_id, issue.reporter.id))
    return group_ids


def _prefixed(group_ids: set[str]) -> ExternalAccess:
    return ExternalAccess(
        external_user_emails=set(),
        external_user_group_ids={
            build_ext_group_name_for_onyx(group_id, DocumentSource.YOUTRACK)
            for group_id in group_ids
        },
        is_public=False,
    )


def issue_access(base_url: str, issue: YouTrackIssueAccess) -> ExternalAccess:
    return _prefixed(issue_group_ids(base_url, issue))


def project_node_access(base_url: str, project_id: str) -> ExternalAccess:
    return _prefixed({project_group_id(base_url, project_id)})
