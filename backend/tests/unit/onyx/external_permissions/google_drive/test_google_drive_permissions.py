"""The Google Drive ACL mapper and the Google group expansion."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from onyx.access.utils import build_domain_group_id
from onyx.connectors.google_drive.models import GoogleGroupMember
from onyx.connectors.google_drive.source_operations import (
    GoogleDriveHttpError,
    GoogleDriveRefreshError,
)
from onyx.external_permissions.google_drive.doc_sync import (
    get_external_access_for_folder,
    get_external_access_for_raw_gdrive_file,
    map_drive_permissions,
)
from onyx.external_permissions.google_drive.group_sync import iter_google_groups


def _http_error(status: int, reasons: tuple[str, ...] = ()) -> GoogleDriveHttpError:
    return GoogleDriveHttpError(
        status_code=status,
        reasons=reasons,
        access_denied=status in (403, 404),
        message=f"HTTP {status}",
    )


_PERMISSIONS: list[dict[str, Any]] = [
    {"type": "user", "emailAddress": "Ann@corp.com", "role": "owner"},
    {"type": "group", "emailAddress": "Eng@corp.com", "role": "reader"},
    {"type": "domain", "domain": "Corp.com", "role": "reader"},
    {"type": "user", "emailAddress": "gone@corp.com", "deleted": True},
]


def test_permissions_map_to_users_groups_and_domain() -> None:
    access = map_drive_permissions(_PERMISSIONS, add_prefix=False)
    assert access.external_user_emails == {"ann@corp.com"}
    assert access.external_user_group_ids == {
        "eng@corp.com",
        build_domain_group_id("corp.com"),
    }
    assert not access.is_public


def test_add_prefix_namespaces_group_ids() -> None:
    access = map_drive_permissions(_PERMISSIONS, add_prefix=True)
    assert access.external_user_group_ids == {
        "google_drive_eng@corp.com",
        "google_drive_domain:corp.com",
    }


def test_anyone_makes_the_file_public() -> None:
    access = map_drive_permissions(
        [{"type": "anyone", "allowFileDiscovery": False}], add_prefix=False
    )
    assert access.is_public


def test_inline_permissions_need_no_call() -> None:
    ops = MagicMock()
    access = get_external_access_for_raw_gdrive_file(
        file={"id": "f1", "permissions": _PERMISSIONS},
        company_domain="corp.com",
        ops=ops,
        retriever_email="ann@corp.com",
        admin_email="admin@corp.com",
        fallback_user_email="lister@corp.com",
    )
    assert access.external_user_emails == {"ann@corp.com"}
    ops.list_file_permissions.assert_not_called()


def test_shared_drive_file_reads_permissions_in_reader_order() -> None:
    calls: list[str] = []

    def list_file_permissions(
        *, user_email: str, file_id: str, fields: str
    ) -> Iterator[dict[str, Any]]:
        calls.append(user_email)
        assert file_id == "f1"
        assert "nextPageToken" in fields
        if user_email == "owner@other.com":
            raise GoogleDriveRefreshError("cannot impersonate")
        if user_email == "lister@corp.com":
            raise _http_error(403)
        return iter([{"type": "user", "emailAddress": "bob@corp.com"}])

    ops = MagicMock()
    ops.list_file_permissions.side_effect = list_file_permissions
    access = get_external_access_for_raw_gdrive_file(
        file={"id": "f1", "permissionIds": ["p1"]},
        company_domain="corp.com",
        ops=ops,
        retriever_email="owner@other.com",
        admin_email="admin@corp.com",
        fallback_user_email="lister@corp.com",
        fallback_retriever_email="lister@corp.com",
    )
    assert calls == ["owner@other.com", "lister@corp.com", "admin@corp.com"]
    assert access.external_user_emails == {"bob@corp.com"}


def test_unreadable_permissions_fall_back_to_the_lister() -> None:
    ops = MagicMock()
    ops.list_file_permissions.side_effect = _http_error(404)
    access = get_external_access_for_raw_gdrive_file(
        file={"id": "f1"},
        company_domain="corp.com",
        ops=ops,
        retriever_email=None,
        admin_email="admin@corp.com",
        fallback_user_email="Lister@corp.com",
    )
    assert access.external_user_emails == {"lister@corp.com"}
    assert not access.external_user_group_ids
    assert not access.is_public


def test_rate_limit_is_raised_not_narrowed() -> None:
    ops = MagicMock()
    ops.list_file_permissions.side_effect = _http_error(403, ("rateLimitExceeded",))
    with pytest.raises(GoogleDriveHttpError):
        get_external_access_for_raw_gdrive_file(
            file={"id": "f1"},
            company_domain="corp.com",
            ops=ops,
            retriever_email="a@corp.com",
            admin_email="admin@corp.com",
            fallback_user_email="a@corp.com",
        )


def test_unreadable_folder_is_private() -> None:
    ops = MagicMock()
    ops.list_file_permissions.side_effect = _http_error(403)
    access = get_external_access_for_folder(
        {"id": "folder", "permissionIds": ["p"]}, "corp.com", ops, "admin@corp.com"
    )
    assert access.num_entries == 0
    assert not access.is_public


def _members(
    table: dict[str, list[GoogleGroupMember]],
    broken: set[str] | None = None,
) -> Any:
    def list_members(group_email: str) -> Iterator[GoogleGroupMember]:
        if broken and group_email in broken:
            raise _http_error(403)
        return iter(table.get(group_email, []))

    return list_members


def test_groups_expand_nested_groups_and_customer() -> None:
    table = {
        "eng@corp.com": [
            GoogleGroupMember(email="Ann@corp.com", type="USER"),
            GoogleGroupMember(email="infra@corp.com", type="GROUP"),
        ],
        "infra@corp.com": [
            GoogleGroupMember(email="bob@corp.com", type="USER"),
            # A cycle stops.
            GoogleGroupMember(email="eng@corp.com", type="GROUP"),
        ],
        "all@corp.com": [GoogleGroupMember(email=None, type="CUSTOMER")],
    }
    groups = {
        group.id: group.user_emails
        for group in iter_google_groups(
            ["Eng@corp.com", "infra@corp.com", "all@corp.com"],
            _members(table),
            lambda: iter(["ann@corp.com", "bob@corp.com", "cy@corp.com"]),
            "corp.com",
        )
    }
    assert groups == {
        build_domain_group_id("corp.com"): [
            "ann@corp.com",
            "bob@corp.com",
            "cy@corp.com",
        ],
        "eng@corp.com": ["ann@corp.com", "bob@corp.com"],
        "infra@corp.com": ["ann@corp.com", "bob@corp.com"],
        "all@corp.com": ["ann@corp.com", "bob@corp.com", "cy@corp.com"],
    }


def test_group_with_unreadable_nested_group_is_skipped_whole() -> None:
    table = {
        "eng@corp.com": [
            GoogleGroupMember(email="ann@corp.com", type="USER"),
            GoogleGroupMember(email="secret@corp.com", type="GROUP"),
        ],
        "ok1@corp.com": [GoogleGroupMember(email="a@corp.com", type="USER")],
        "ok2@corp.com": [GoogleGroupMember(email="b@corp.com", type="USER")],
    }
    ids = [
        group.id
        for group in iter_google_groups(
            ["eng@corp.com", "ok1@corp.com", "ok2@corp.com"],
            _members(table, broken={"secret@corp.com"}),
            lambda: iter([]),
            "corp.com",
        )
    ]
    assert ids == [build_domain_group_id("corp.com"), "ok1@corp.com", "ok2@corp.com"]


def test_most_groups_unreadable_fails_the_sync() -> None:
    with pytest.raises(RuntimeError, match="2 of 3"):
        list(
            iter_google_groups(
                ["a@corp.com", "b@corp.com", "c@corp.com"],
                _members({}, broken={"a@corp.com", "b@corp.com"}),
                lambda: iter([]),
                "corp.com",
            )
        )
