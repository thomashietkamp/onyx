from typing import Any
from unittest.mock import MagicMock

import pytest

from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.github.connector import GithubConnector
from onyx.connectors.github.models import GitHubRepository, GitHubUser
from onyx.connectors.github.source_operations import GitHubApiError
from onyx.connectors.github.utils import (
    GITHUB_COM_API_URL,
    get_external_access_permission,
)
from onyx.db.external_perm import prefixed_group_id
from onyx.external_permissions.github.group_sync import iter_github_groups
from onyx.external_permissions.github.utils import (
    organization_members_group_id,
    repository_collaborators_group_id,
)


def _repo(
    repo_id: int, visibility: str | None, org: dict[str, Any] | None = None
) -> GitHubRepository:
    raw: dict[str, Any] = {
        "id": repo_id,
        "name": f"r{repo_id}",
        "full_name": f"acme/r{repo_id}",
        "html_url": f"https://github.com/acme/r{repo_id}",
        "visibility": visibility,
        "private": visibility != "public",
        "owner": {"login": "acme", "type": "Organization" if org else "User"},
    }
    if org:
        raw["organization"] = org
    return GitHubRepository.from_raw(raw)


_ORG = {"id": 7, "login": "acme"}
_GHES = "https://ghes.example.com/api/v3"


def _prefixed(group_id: str) -> str:
    return build_ext_group_name_for_onyx(group_id, DocumentSource.GITHUB)


def test_public_repository_is_public() -> None:
    access = get_external_access_permission(
        _repo(1, "public", _ORG), GITHUB_COM_API_URL
    )
    assert access.is_public
    assert not access.external_user_group_ids


@pytest.mark.parametrize("visibility", ["private", None])
def test_private_repository_names_collaborators(visibility: str | None) -> None:
    access = get_external_access_permission(
        _repo(2, visibility, _ORG), GITHUB_COM_API_URL
    )
    assert not access.is_public
    assert access.external_user_group_ids == {
        _prefixed(repository_collaborators_group_id(GITHUB_COM_API_URL, 2))
    }


def test_internal_repository_names_organization() -> None:
    access = get_external_access_permission(
        _repo(3, "internal", _ORG), GITHUB_COM_API_URL
    )
    assert access.external_user_group_ids == {
        _prefixed(organization_members_group_id(GITHUB_COM_API_URL, 7))
    }


def test_internal_repository_without_organization_uses_collaborators() -> None:
    access = get_external_access_permission(_repo(4, "internal"), GITHUB_COM_API_URL)
    assert access.external_user_group_ids == {
        _prefixed(repository_collaborators_group_id(GITHUB_COM_API_URL, 4))
    }


@pytest.mark.parametrize("visibility", ["private", "internal"])
def test_same_ids_on_two_instances_name_different_groups(visibility: str) -> None:
    """A repository or organization id of github.com can repeat on an
    Enterprise Server. Users of one must not read the other's documents."""
    repo = _repo(2, visibility, _ORG)
    on_github_com = get_external_access_permission(repo, GITHUB_COM_API_URL)
    on_ghes = get_external_access_permission(repo, _GHES)
    assert on_github_com.external_user_group_ids
    assert on_ghes.external_user_group_ids
    assert not on_github_com.external_user_group_ids & on_ghes.external_user_group_ids


def _users(login: str) -> GitHubUser:
    if login == "ghost":
        raise GitHubApiError("not found", status_code=404)
    emails = {"ann": "Ann@Example.com", "bob": "bob@example.com"}
    return GitHubUser(login=login, email=emails.get(login))


def test_group_sync_matches_document_group_ids() -> None:
    repos = [
        _repo(1, "public", _ORG),
        _repo(2, "private", _ORG),
        _repo(3, "internal", _ORG),
        _repo(5, "internal", _ORG),
    ]
    ops = MagicMock()
    ops.list_repository_collaborators.return_value = ["ann", "nomail", "ghost"]
    ops.list_organization_members.return_value = ["ann", "bob"]
    ops.get_user.side_effect = lambda login: _users(login)
    connector = MagicMock(spec=GithubConnector)
    connector.source_operations = ops
    connector.instance_url = _GHES
    connector.fetch_configured_repos.return_value = repos

    groups = list(iter_github_groups(connector))

    assert [(g.id, g.user_emails) for g in groups] == [
        (repository_collaborators_group_id(_GHES, 2), ["ann@example.com"]),
        (
            organization_members_group_id(_GHES, 7),
            ["ann@example.com", "bob@example.com"],
        ),
    ]
    # One members listing per organization, one profile read per login.
    ops.list_organization_members.assert_called_once_with(org_login="acme")
    assert ops.get_user.call_count == 4
    doc_groups = {
        group_id
        for repo in repos
        for group_id in get_external_access_permission(
            repo, _GHES
        ).external_user_group_ids
    }
    assert {prefixed_group_id(g.id, DocumentSource.GITHUB) for g in groups} == {
        prefixed_group_id(g, DocumentSource.GITHUB) for g in doc_groups
    }
