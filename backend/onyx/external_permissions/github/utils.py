"""Who may read the documents of a GitHub repository. The GitHub connector
reaches this through ``onyx/connectors/github/utils.py``.

Every document of a repository has the repository's access. A public
repository is public. An internal repository is readable by the members of
its organization. A private repository, or one whose visibility is unknown, is
readable by its collaborators (every grant: direct, team, organization base
permission). The group sync supplies the members of both group kinds.
"""

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.github.models import GitHubRepository

VISIBILITY_PUBLIC = "public"
VISIBILITY_INTERNAL = "internal"


def repository_collaborators_group_id(repo_id: int) -> str:
    return f"repo_{repo_id}_collaborators"


def organization_members_group_id(org_id: int) -> str:
    return f"org_{org_id}_members"


def is_collaborator_scoped(repo: GitHubRepository) -> bool:
    """True when the collaborators group decides who reads the repository."""
    if repo.visibility == VISIBILITY_PUBLIC:
        return False
    return not (repo.visibility == VISIBILITY_INTERNAL and repo.organization)


def get_external_access_permission(
    repo: GitHubRepository, add_prefix: bool
) -> ExternalAccess:
    if repo.visibility == VISIBILITY_PUBLIC:
        return ExternalAccess(
            external_user_emails=set(),
            external_user_group_ids=set(),
            is_public=True,
        )
    if repo.visibility == VISIBILITY_INTERNAL and repo.organization is not None:
        group_id = organization_members_group_id(repo.organization.id)
    else:
        group_id = repository_collaborators_group_id(repo.id)
    if add_prefix:
        group_id = build_ext_group_name_for_onyx(group_id, DocumentSource.GITHUB)
    return ExternalAccess(
        external_user_emails=set(),
        external_user_group_ids={group_id},
        is_public=False,
    )
