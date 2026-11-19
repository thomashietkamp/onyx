from typing import Any
from unittest.mock import MagicMock

import pytest

from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.github.connector import GithubConnector
from onyx.connectors.github.models import GitHubRepository, GitHubUser
from onyx.connectors.github.source_operations import GitHubApiError
from onyx.connectors.github.utils import get_external_access_permission
from onyx.db.external_perm import prefixed_group_id
from onyx.external_permissions.github.group_sync import iter_github_groups


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


def test_public_repository_is_public() -> None:
    access = get_external_access_permission(_repo(1, "public", _ORG))
    assert access.is_public
    assert not access.external_user_group_ids


@pytest.mark.parametrize("visibility", ["private", None])
def test_private_repository_names_collaborators(visibility: str | None) -> None:
    access = get_external_access_permission(_repo(2, visibility, _ORG))
    assert not access.is_public
    assert access.external_user_group_ids == {
        build_ext_group_name_for_onyx("repo_2_collaborators", DocumentSource.GITHUB)
    }


def test_internal_repository_names_organization() -> None:
    access = get_external_access_permission(_repo(3, "internal", _ORG))
    assert access.external_user_group_ids == {"github_org_7_members"}


def test_internal_repository_without_organization_uses_collaborators() -> None:
    access = get_external_access_permission(_repo(4, "internal"))
    assert access.external_user_group_ids == {"github_repo_4_collaborators"}


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
    connector.fetch_configured_repos.return_value = repos

    groups = list(iter_github_groups(connector))

    assert [(g.id, g.user_emails) for g in groups] == [
        ("repo_2_collaborators", ["ann@example.com"]),
        ("org_7_members", ["ann@example.com", "bob@example.com"]),
    ]
    # One members listing per organization, one profile read per login.
    ops.list_organization_members.assert_called_once_with(org_login="acme")
    assert ops.get_user.call_count == 4
    doc_groups = {
        group_id
        for repo in repos
        for group_id in get_external_access_permission(repo).external_user_group_ids
    }
    assert {prefixed_group_id(g.id, DocumentSource.GITHUB) for g in groups} == {
        prefixed_group_id(g, DocumentSource.GITHUB) for g in doc_groups
    }
