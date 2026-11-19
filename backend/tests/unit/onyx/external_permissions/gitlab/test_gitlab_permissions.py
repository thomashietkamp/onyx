from typing import Any
from unittest.mock import MagicMock

import pytest
from gitlab.exceptions import GitlabGetError

from onyx.configs.constants import DocumentSource
from onyx.connectors.gitlab.access import (
    get_code_access,
    get_issue_access,
    get_merge_request_access,
)
from onyx.connectors.gitlab.connector import (
    GitlabConnector,
    gitlab_user_email,
    is_gitlab_com,
)
from onyx.connectors.gitlab.models import GitlabIssueReaders, GitlabProjectAccessInfo
from onyx.connectors.models import SlimDocument
from onyx.db.external_perm import prefixed_group_id
from onyx.external_permissions.gitlab.access import (
    ROLE_GUEST,
    ROLE_REPORTER,
    instance_users_group_id,
    project_role_group_id,
)
from onyx.external_permissions.gitlab.group_sync import iter_gitlab_groups

_SELF_MANAGED = "https://gitlab.example.com"
_GITLAB_COM = "https://gitlab.com"


def _prefixed(group_id: str) -> str:
    return prefixed_group_id(group_id, DocumentSource.GITLAB)


def _reporter(url: str = _SELF_MANAGED) -> str:
    return _prefixed(project_role_group_id(url, 42, ROLE_REPORTER))


def _guest(url: str = _SELF_MANAGED) -> str:
    return _prefixed(project_role_group_id(url, 42, ROLE_GUEST))


def _instance(url: str = _SELF_MANAGED) -> str:
    return _prefixed(instance_users_group_id(url))


def _info(
    visibility: str | None,
    url: str = _SELF_MANAGED,
    repository: str | None = "enabled",
    merge_requests: str | None = "enabled",
    issues: str | None = "enabled",
) -> GitlabProjectAccessInfo:
    return GitlabProjectAccessInfo(
        instance_url=url,
        is_gitlab_com=is_gitlab_com(url),
        project_id=42,
        visibility=visibility,
        repository_access_level=repository,
        merge_requests_access_level=merge_requests,
        issues_access_level=issues,
    )


_OPEN_ISSUE = GitlabIssueReaders(confidential=False)


# --- visibility mapping ---------------------------------------------------------


@pytest.mark.parametrize("url", [_SELF_MANAGED, _GITLAB_COM])
def test_public_project_documents_are_public(url: str) -> None:
    info = _info("public", url=url)
    for access in (
        get_code_access(info),
        get_merge_request_access(info),
        get_issue_access(info, _OPEN_ISSUE),
    ):
        assert access.is_public
        assert not access.external_user_group_ids
        assert not access.external_user_emails


def test_internal_project_on_self_managed_adds_instance_users() -> None:
    info = _info("internal")
    assert get_code_access(info).external_user_group_ids == {
        _reporter(),
        _instance(),
    }
    assert get_merge_request_access(info).external_user_group_ids == {
        _reporter(),
        _instance(),
    }
    assert get_issue_access(info, _OPEN_ISSUE).external_user_group_ids == {
        _guest(),
        _instance(),
    }
    assert not get_code_access(info).is_public


def test_internal_project_on_gitlab_com_is_members_only() -> None:
    info = _info("internal", url=_GITLAB_COM)
    assert get_code_access(info).external_user_group_ids == {_reporter(_GITLAB_COM)}
    assert get_issue_access(info, _OPEN_ISSUE).external_user_group_ids == {
        _guest(_GITLAB_COM)
    }


@pytest.mark.parametrize("visibility", ["private", None, "unexpected"])
def test_private_or_unknown_project_needs_membership(visibility: str | None) -> None:
    info = _info(visibility)
    code = get_code_access(info)
    assert not code.is_public
    # Code files and merge requests need Reporter, issues need Guest.
    assert code.external_user_group_ids == {_reporter()}
    assert get_merge_request_access(info).external_user_group_ids == {_reporter()}
    assert get_issue_access(info, _OPEN_ISSUE).external_user_group_ids == {_guest()}


# --- feature access levels ------------------------------------------------------


@pytest.mark.parametrize("level", ["private", "disabled", None])
@pytest.mark.parametrize("visibility", ["public", "internal"])
def test_restricted_features_fall_back_to_member_groups(
    level: str | None, visibility: str
) -> None:
    assert get_code_access(
        _info(visibility, repository=level)
    ).external_user_group_ids == {_reporter()}
    assert get_merge_request_access(
        _info(visibility, merge_requests=level)
    ).external_user_group_ids == {_reporter()}
    assert get_issue_access(
        _info(visibility, issues=level), _OPEN_ISSUE
    ).external_user_group_ids == {_guest()}


def test_merge_requests_follow_a_restricted_repository() -> None:
    access = get_merge_request_access(_info("public", repository="private"))
    assert not access.is_public
    assert access.external_user_group_ids == {_reporter()}


def test_restricted_repository_leaves_issues_open() -> None:
    assert get_issue_access(
        _info("public", repository="private"), _OPEN_ISSUE
    ).is_public


# --- confidential issues --------------------------------------------------------


@pytest.mark.parametrize("visibility", ["public", "internal", "private"])
def test_confidential_issue_needs_reporter_or_author_or_assignee(
    visibility: str,
) -> None:
    issue = GitlabIssueReaders(
        confidential=True,
        author_and_assignee_emails=frozenset({"ann@example.com", "bob@example.com"}),
    )
    access = get_issue_access(_info(visibility), issue)
    assert not access.is_public
    assert access.external_user_group_ids == {_reporter()}
    assert access.external_user_emails == {"ann@example.com", "bob@example.com"}


def _connector(url: str = _SELF_MANAGED) -> tuple[GitlabConnector, MagicMock]:
    connector = GitlabConnector(project_owner="acme", project_name="app")
    client = MagicMock()
    client.url = url
    connector.gitlab_client = client
    return connector, client


def _project(visibility: str) -> MagicMock:
    project = MagicMock()
    project.attributes = {
        "id": 42,
        "visibility": visibility,
        "repository_access_level": "enabled",
        "merge_requests_access_level": "enabled",
        "issues_access_level": "enabled",
    }
    return project


def _issue(confidential: bool) -> MagicMock:
    raw: dict[str, Any] = {
        "web_url": f"{_SELF_MANAGED}/acme/app/-/issues/{int(confidential)}",
        "created_at": "2024-05-01T10:00:00Z",
        "confidential": confidential,
        "author": {"id": 1, "username": "ann"},
        "assignees": [{"id": 2, "username": "bob"}, {"id": 3, "username": "gone"}],
    }
    issue = MagicMock()
    for key, value in raw.items():
        setattr(issue, key, value)
    issue.attributes = raw
    return issue


def _user(user_id: int) -> MagicMock:
    if user_id == 3:
        raise GitlabGetError("404 Not found", response_code=404)
    return MagicMock(
        attributes={
            1: {"id": 1, "email": "Ann@Example.com", "public_email": "x@y.z"},
            2: {"id": 2, "public_email": "bob@example.com"},
        }[user_id]
    )


def test_perm_sync_listing_resolves_confidential_issue_readers() -> None:
    connector, client = _connector()
    connector.include_code_files = False
    connector.include_mrs = False
    project = _project("public")
    project.issues.list.return_value = [_issue(False), _issue(True)]
    client.projects.get.return_value = project
    client.users.get.side_effect = _user

    docs = [
        doc
        for batch in connector.retrieve_all_slim_docs_perm_sync()
        for doc in batch
        if isinstance(doc, SlimDocument)
    ]

    open_issue, confidential = docs
    assert open_issue.external_access is not None
    assert open_issue.external_access.is_public
    assert confidential.external_access is not None
    assert not confidential.external_access.is_public
    assert confidential.external_access.external_user_emails == {
        "ann@example.com",
        "bob@example.com",
    }
    # Only the confidential issue needs user lookups.
    assert client.users.get.call_count == 3


def test_user_lookup_raises_on_errors_other_than_not_found() -> None:
    connector, client = _connector()
    client.users.get.side_effect = GitlabGetError("500", response_code=500)
    with pytest.raises(GitlabGetError):
        connector.email_resolver().email(1)


# --- email fallback -------------------------------------------------------------


@pytest.mark.parametrize(
    "attributes, expected",
    [
        (
            {"email": " Admin@Example.com ", "public_email": "p@example.com"},
            "admin@example.com",
        ),
        ({"email": "", "public_email": "Pub@Example.com"}, "pub@example.com"),
        ({"public_email": "pub@example.com"}, "pub@example.com"),
        ({"email": None, "public_email": ""}, None),
        ({}, None),
    ],
)
def test_email_prefers_admin_email_then_public_email(
    attributes: dict[str, Any], expected: str | None
) -> None:
    assert gitlab_user_email(attributes) == expected


# --- group sync -----------------------------------------------------------------


def _member(user_id: int, level: int, **extra: Any) -> MagicMock:
    return MagicMock(attributes={"id": user_id, "access_level": level, **extra})


def _members() -> list[MagicMock]:
    return [
        _member(1, 50, email="owner@example.com", state="active"),
        _member(2, 30, public_email="dev@example.com"),
        _member(3, 20),  # email from /users/:id
        _member(4, 10, public_email="guest@example.com"),
        _member(5, 5, public_email="minimal@example.com"),
        _member(6, 40, public_email="blocked@example.com", state="blocked"),
        _member(7, 30, public_email="invited@example.com", membership_state="awaiting"),
        _member(8, 30),  # no email anywhere
    ]


def _lookup(user_id: int) -> MagicMock:
    return MagicMock(
        attributes={3: {"public_email": "reporter@example.com"}}.get(user_id, {})
    )


def _instance_users() -> list[MagicMock]:
    return [
        MagicMock(
            attributes={
                "id": 1,
                "email": "owner@example.com",
                "state": "active",
                "external": False,
            }
        ),
        MagicMock(
            attributes={
                "id": 9,
                "email": "staff@example.com",
                "state": "active",
                "external": False,
            }
        ),
        MagicMock(
            attributes={
                "id": 10,
                "email": "ext@example.com",
                "state": "active",
                "external": True,
            }
        ),
        MagicMock(
            attributes={
                "id": 11,
                "email": "bot@example.com",
                "state": "active",
                "external": False,
                "bot": True,
            }
        ),
        MagicMock(
            attributes={
                "id": 12,
                "email": "off@example.com",
                "state": "deactivated",
                "external": False,
            }
        ),
        # Non-admin view: no "external" field, so the user is not admitted.
        MagicMock(
            attributes={
                "id": 13,
                "public_email": "unknown@example.com",
                "state": "active",
            }
        ),
    ]


def _group_sync(
    visibility: str, url: str = _SELF_MANAGED
) -> tuple[dict[str, list[str]], MagicMock]:
    connector, client = _connector(url)
    project = _project(visibility)
    project.members_all.list.return_value = _members()
    client.projects.get.return_value = project
    client.users.get.side_effect = _lookup
    client.users.list.return_value = _instance_users()
    groups = {group.id: group.user_emails for group in iter_gitlab_groups(connector)}
    return groups, client


def test_group_sync_fills_member_groups_by_access_level() -> None:
    groups, client = _group_sync("private")

    assert groups == {
        project_role_group_id(_SELF_MANAGED, 42, ROLE_REPORTER): [
            "dev@example.com",
            "owner@example.com",
            "reporter@example.com",
        ],
        project_role_group_id(_SELF_MANAGED, 42, ROLE_GUEST): [
            "dev@example.com",
            "guest@example.com",
            "owner@example.com",
            "reporter@example.com",
        ],
    }
    # Only members without an email in the listing need a lookup.
    looked_up = sorted(call.args[0] for call in client.users.get.call_args_list)
    assert looked_up == [3, 8]
    client.users.list.assert_not_called()


def test_group_sync_adds_instance_users_for_internal_self_managed_project() -> None:
    groups, _ = _group_sync("internal")
    assert groups[instance_users_group_id(_SELF_MANAGED)] == [
        "owner@example.com",
        "staff@example.com",
    ]


def test_group_sync_never_lists_gitlab_com_users() -> None:
    groups, client = _group_sync("internal", url=_GITLAB_COM)
    assert instance_users_group_id(_GITLAB_COM) not in groups
    client.users.list.assert_not_called()


@pytest.mark.parametrize("visibility", ["public", "internal", "private"])
@pytest.mark.parametrize("url", [_SELF_MANAGED, _GITLAB_COM])
def test_group_ids_match_document_group_ids(visibility: str, url: str) -> None:
    groups, _ = _group_sync(visibility, url=url)
    synced = {prefixed_group_id(group_id, DocumentSource.GITLAB) for group_id in groups}

    info = _info(visibility, url=url)
    confidential = GitlabIssueReaders(confidential=True)
    restricted = _info(visibility, url=url, repository="private", issues="private")
    document_groups = {
        prefixed_group_id(group_id, DocumentSource.GITLAB)
        for access in (
            get_code_access(info),
            get_merge_request_access(info),
            get_issue_access(info, _OPEN_ISSUE),
            get_issue_access(info, confidential),
            get_code_access(restricted),
            get_issue_access(restricted, _OPEN_ISSUE),
        )
        for group_id in access.external_user_group_ids
    }
    # The doc side stores ids already prefixed; the prefix is idempotent.
    assert document_groups <= synced


def test_group_ids_differ_between_instances() -> None:
    assert project_role_group_id(
        _SELF_MANAGED, 42, ROLE_GUEST
    ) != project_role_group_id(_GITLAB_COM, 42, ROLE_GUEST)
    assert project_role_group_id(
        "https://GitLab.example.com/", 42, ROLE_GUEST
    ) == project_role_group_id(_SELF_MANAGED, 42, ROLE_GUEST)
