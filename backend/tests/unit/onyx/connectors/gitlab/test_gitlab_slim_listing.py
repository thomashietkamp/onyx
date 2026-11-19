"""The GitLab slim listing that pruning and doc sync read must list exactly
the ids that indexing produces, for every document kind."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from onyx.background.celery.celery_utils import extract_ids_from_runnable_connector
from onyx.connectors.gitlab.connector import GitlabConnector
from onyx.connectors.interfaces import prune_listing_honors_indexing_start
from onyx.connectors.models import Document, SlimDocument

_INSTANCE_URL = "https://gitlab.example.com"
_TIMESTAMP = "2024-05-01T10:00:00.000+00:00"

_TREE: dict[str, list[dict[str, Any]]] = {
    "": [
        {"id": "blob-readme", "path": "README.md", "name": "README.md", "type": "blob"},
        {"id": "tree-src", "path": "src", "name": "src", "type": "tree"},
        {"id": "tree-logs", "path": "logs", "name": "logs", "type": "tree"},
        {
            "id": "blob-precommit",
            "path": ".pre-commit-config.yaml",
            "name": ".pre-commit-config.yaml",
            "type": "blob",
        },
    ],
    "src": [
        {"id": "blob-main", "path": "src/main.py", "name": "main.py", "type": "blob"},
        {"id": "tree-pkg", "path": "src/pkg", "name": "pkg", "type": "tree"},
    ],
    "src/pkg": [
        {
            "id": "blob-mod",
            "path": "src/pkg/mod.py",
            "name": "mod.py",
            "type": "blob",
        },
    ],
    "logs": [
        {"id": "blob-log", "path": "logs/a.log", "name": "a.log", "type": "blob"},
    ],
}


def _item(kind: str, iid: int, **attributes: Any) -> MagicMock:
    web_url = f"{_INSTANCE_URL}/acme/app/-/{kind}/{iid}"
    raw: dict[str, Any] = {
        "iid": iid,
        "web_url": web_url,
        "title": f"{kind} {iid}",
        "description": "body",
        "updated_at": _TIMESTAMP,
        "created_at": _TIMESTAMP,
        "author": {"id": 100 + iid, "name": "Ann", "username": "ann"},
        "state": "opened",
        "type": None,
        "confidential": False,
        "assignees": [],
    }
    raw.update(attributes)
    item = MagicMock()
    for key, value in raw.items():
        setattr(item, key, value)
    item.attributes = raw
    return item


def _project(project_attributes: dict[str, Any] | None = None) -> MagicMock:
    project = MagicMock()
    project.default_branch = "main"
    project.attributes = {
        "id": 42,
        "visibility": "private",
        "repository_access_level": "enabled",
        "merge_requests_access_level": "enabled",
        "issues_access_level": "enabled",
        **(project_attributes or {}),
    }
    project.repository_tree.side_effect = lambda path, all: list(  # noqa: ARG005
        _TREE[path]
    )
    content = MagicMock()
    content.decode.return_value = b"print('hi')"
    project.files.get.return_value = content
    project.mergerequests.list.return_value = [_item("merge_requests", 1)]
    project.issues.list.return_value = [
        _item("issues", 1),
        _item("issues", 2, confidential=True),
    ]
    return project


def _connector(
    project: MagicMock, batch_size: int = 2, **flags: bool
) -> GitlabConnector:
    connector = GitlabConnector(
        project_owner="acme",
        project_name="app",
        batch_size=batch_size,
        include_code_files=flags.get("include_code_files", True),
        include_mrs=flags.get("include_mrs", True),
        include_issues=flags.get("include_issues", True),
    )
    client = MagicMock()
    client.url = _INSTANCE_URL
    client.projects.get.return_value = project
    client.users.get.side_effect = lambda user_id: MagicMock(
        attributes={"id": user_id, "public_email": f"u{user_id}@example.com"}
    )
    connector.gitlab_client = client
    return connector


def _indexed_ids(connector: GitlabConnector) -> list[str]:
    return [
        doc.id
        for batch in connector.load_from_state()
        for doc in batch
        if isinstance(doc, Document)
    ]


def _slim_ids(connector: GitlabConnector, perm_sync: bool) -> list[str]:
    listing = (
        connector.retrieve_all_slim_docs_perm_sync()
        if perm_sync
        else connector.retrieve_all_slim_docs()
    )
    return [
        doc.id for batch in listing for doc in batch if isinstance(doc, SlimDocument)
    ]


@pytest.mark.parametrize("perm_sync", [False, True])
def test_slim_ids_equal_indexing_ids_for_every_kind(perm_sync: bool) -> None:
    indexed = _indexed_ids(_connector(_project()))
    listed = _slim_ids(_connector(_project()), perm_sync=perm_sync)

    assert listed == indexed
    # Code files by blob id, merge requests and issues by web URL.
    assert set(listed) == {
        "blob-readme",
        "blob-main",
        "blob-mod",
        f"{_INSTANCE_URL}/acme/app/-/merge_requests/1",
        f"{_INSTANCE_URL}/acme/app/-/issues/1",
        f"{_INSTANCE_URL}/acme/app/-/issues/2",
    }


@pytest.mark.parametrize(
    "flags, expected_prefixes",
    [
        ({"include_code_files": False}, {"merge_requests", "issues"}),
        ({"include_mrs": False}, {"blob", "issues"}),
        ({"include_issues": False}, {"blob", "merge_requests"}),
    ],
)
def test_slim_listing_honors_document_kind_flags(
    flags: dict[str, bool], expected_prefixes: set[str]
) -> None:
    indexed = _indexed_ids(_connector(_project(), **flags))
    listed = _slim_ids(_connector(_project(), **flags), perm_sync=False)

    assert listed == indexed

    def kind(doc_id: str) -> str:
        return (
            "blob"
            if doc_id.startswith("blob")
            else doc_id.split("/-/")[1].split("/")[0]
        )

    assert {kind(doc_id) for doc_id in listed} == expected_prefixes


def test_slim_listing_reads_no_file_contents_and_uses_the_state_filter() -> None:
    project = _project()
    connector = _connector(project)
    connector.state_filter = "opened"

    _slim_ids(connector, perm_sync=False)

    project.files.get.assert_not_called()
    project.mergerequests.list.assert_called_once_with(state="opened", iterator=True)
    project.issues.list.assert_called_once_with(state="opened", iterator=True)


def test_plain_slim_listing_has_no_access_and_perm_sync_listing_has_access() -> None:
    plain = [
        doc
        for batch in _connector(_project()).retrieve_all_slim_docs()
        for doc in batch
        if isinstance(doc, SlimDocument)
    ]
    synced = [
        doc
        for batch in _connector(_project()).retrieve_all_slim_docs_perm_sync()
        for doc in batch
        if isinstance(doc, SlimDocument)
    ]

    assert all(doc.external_access is None for doc in plain)
    assert all(doc.external_access is not None for doc in synced)


def test_slim_listing_raises_when_the_project_is_unreadable() -> None:
    connector = _connector(_project())
    client = MagicMock()
    client.projects.get.side_effect = RuntimeError("401")
    connector.gitlab_client = client

    with pytest.raises(RuntimeError):
        _slim_ids(connector, perm_sync=False)


def test_prune_uses_the_slim_listing_of_every_document() -> None:
    project = _project()
    connector = _connector(project)

    result = extract_ids_from_runnable_connector(
        connector, connector_type="gitlab", start=1_700_000_000.0
    )

    assert set(result.raw_id_to_parent) == set(_indexed_ids(_connector(_project())))
    # The listing ignores the indexing start, so prune lists every document.
    assert not prune_listing_honors_indexing_start(GitlabConnector)
    assert result.listed_from is None
    project.files.get.assert_not_called()
