from pydantic import BaseModel, ConfigDict

# GitLab project and feature settings, as the REST API v4 returns them.
VISIBILITY_PUBLIC = "public"
VISIBILITY_INTERNAL = "internal"
FEATURE_ENABLED = "enabled"

# Effective access levels of /projects/:id/members/all.
GUEST_ACCESS_LEVEL = 10
REPORTER_ACCESS_LEVEL = 20

USER_STATE_ACTIVE = "active"


class GitlabProjectAccessInfo(BaseModel):
    """What decides who may read the documents of one GitLab project."""

    model_config = ConfigDict(frozen=True)

    instance_url: str
    is_gitlab_com: bool
    project_id: int
    # "public", "internal" or "private". None when the token cannot read it.
    visibility: str | None
    # "enabled", "private" (members only) or "disabled". None when unknown.
    repository_access_level: str | None
    merge_requests_access_level: str | None
    issues_access_level: str | None


class GitlabIssueReaders(BaseModel):
    """The issue fields that change who may read it."""

    model_config = ConfigDict(frozen=True)

    confidential: bool
    # Resolved emails of the author and the assignees. Only a confidential
    # issue uses them.
    author_and_assignee_emails: frozenset[str] = frozenset()


class GitlabMember(BaseModel):
    """One entry of /projects/:id/members/all: the effective access of a user,
    direct, inherited from a parent group, or from a shared group."""

    model_config = ConfigDict(frozen=True)

    user_id: int
    access_level: int
    state: str | None = None
    membership_state: str | None = None
    email: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state in (None, USER_STATE_ACTIVE) and self.membership_state in (
            None,
            USER_STATE_ACTIVE,
        )


class GitlabInstanceUser(BaseModel):
    """One entry of /users."""

    model_config = ConfigDict(frozen=True)

    user_id: int
    email: str | None
    state: str | None
    bot: bool
    # Admin-only field. None when the token is not an admin.
    external: bool | None
