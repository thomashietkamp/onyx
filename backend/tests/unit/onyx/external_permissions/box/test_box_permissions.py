"""The Box ACL resolver and the Box group sync, against a fake Box client."""

from datetime import datetime, timezone
from typing import cast

from box_sdk_gen import BoxClient
from box_sdk_gen.schemas.collaboration import Collaboration, CollaborationStatusField
from box_sdk_gen.schemas.file_full import FileFull
from box_sdk_gen.schemas.folder import (
    FolderSharedLinkEffectiveAccessField,
    FolderSharedLinkEffectivePermissionField,
    FolderSharedLinkField,
)
from box_sdk_gen.schemas.folder_full import FolderFull
from box_sdk_gen.schemas.folder_mini import FolderMini
from box_sdk_gen.schemas.group_full import GroupFull
from box_sdk_gen.schemas.group_mini import GroupMini, GroupMiniGroupTypeField
from box_sdk_gen.schemas.items import Items
from box_sdk_gen.schemas.user_collaborations import UserCollaborations
from box_sdk_gen.schemas.user_mini import UserMini
from box_sdk_gen.schemas.web_link import (
    WebLink,
    WebLinkSharedLinkEffectiveAccessField,
    WebLinkSharedLinkEffectivePermissionField,
    WebLinkSharedLinkField,
)

from onyx.access.models import ExternalAccess
from onyx.configs.constants import DocumentSource
from onyx.connectors.box.connector import BoxConnector
from onyx.connectors.models import HierarchyNode, SlimDocument
from onyx.db.external_perm import prefixed_group_id
from onyx.external_permissions.box.access import (
    resolve_box_ancestor_access,
    resolve_box_file_access,
    resolve_box_folder_access,
    resolve_box_web_link_access,
)
from onyx.external_permissions.box.group_sync import iter_box_groups
from tests.unit.onyx.connectors.box.fake_box_client import FakeBoxClient

_ENTERPRISE = "ent"
_OWNER = UserMini(id="u1", login="Owner@Example.com")


def _user_collab(login: str, status: CollaborationStatusField) -> Collaboration:
    return Collaboration(
        id=f"c-{login}",
        accessible_by=UserCollaborations(id=f"id-{login}", login=login),
        status=status,
    )


def _group_collab(
    group_id: str,
    group_type: GroupMiniGroupTypeField = GroupMiniGroupTypeField.MANAGED_GROUP,
) -> Collaboration:
    return Collaboration(
        id=f"c-{group_id}",
        accessible_by=GroupMini(id=group_id, group_type=group_type),
        status=CollaborationStatusField.ACCEPTED,
    )


def _folder_link(
    access: FolderSharedLinkEffectiveAccessField,
) -> FolderSharedLinkField:
    return FolderSharedLinkField(
        url="https://app.box.com/s/x",
        effective_access=access,
        effective_permission=FolderSharedLinkEffectivePermissionField.CAN_PREVIEW,
        is_password_enabled=False,
        download_count=0,
        preview_count=0,
    )


def _client(**kwargs: object) -> FakeBoxClient:
    return FakeBoxClient(
        folders_by_id=cast(dict, kwargs.pop("folders_by_id", {})),
        pages=cast(dict, kwargs.pop("pages", {})),
        **cast(dict, kwargs),
    )


def _group(name: str) -> str:
    return prefixed_group_id(name, DocumentSource.BOX)


def test_folder_access_unions_inherited_owner_link_and_accepted_collaborations() -> (
    None
):
    fake = _client(
        folders_by_id={
            "10": FolderFull(
                id="10",
                owned_by=_OWNER,
                shared_link=_folder_link(FolderSharedLinkEffectiveAccessField.COMPANY),
            )
        },
        folder_collaborations={
            "10": [
                _user_collab("Bob@Example.com", CollaborationStatusField.ACCEPTED),
                _user_collab("pending@example.com", CollaborationStatusField.PENDING),
                _group_collab("77"),
            ]
        },
    )
    inherited = ExternalAccess(
        external_user_emails={"carol@example.com"},
        external_user_group_ids=set(),
        is_public=False,
    )

    access = resolve_box_folder_access(
        cast(BoxClient, fake), "10", inherited, _ENTERPRISE
    )

    assert access is not None
    assert access.external_user_emails == {
        "owner@example.com",
        "bob@example.com",
        "carol@example.com",
    }
    assert access.external_user_group_ids == {
        "box_box-group-77",
        "box_box-enterprise-all-users-ent",
    }
    assert access.is_public is False


def test_root_folder_has_no_collaboration_query() -> None:
    fake = _client(folders_by_id={"0": FolderFull(id="0", owned_by=_OWNER)})

    access = resolve_box_folder_access(cast(BoxClient, fake), "0", None, _ENTERPRISE)

    assert access is not None
    assert access.external_user_emails == {"owner@example.com"}
    assert fake.list_collaborations.folder_collaboration_calls == []


def test_ancestor_access_skips_root_and_reads_each_folder() -> None:
    fake = _client(
        folders_by_id={
            "0": FolderFull(id="0"),
            "5": FolderFull(
                id="5",
                owned_by=_OWNER,
                shared_link=_folder_link(FolderSharedLinkEffectiveAccessField.OPEN),
            ),
        },
        folder_collaborations={
            "5": [
                _group_collab("all", GroupMiniGroupTypeField.ALL_USERS_GROUP),
            ]
        },
    )

    access = resolve_box_ancestor_access(
        cast(BoxClient, fake),
        [FolderMini(id="0"), FolderMini(id="5")],
        _ENTERPRISE,
    )

    assert access is not None
    assert access.is_public is True
    assert access.external_user_group_ids == {"box_box-enterprise-all-users-ent"}
    assert fake.list_collaborations.folder_collaboration_calls == ["5"]


def test_file_access_adds_file_collaborations_only_when_present() -> None:
    fake = _client(
        file_collaborations={
            "1": [_user_collab("dave@example.com", CollaborationStatusField.ACCEPTED)]
        }
    )
    folder_access = ExternalAccess.empty()
    shared = FileFull(id="1", owned_by=_OWNER, has_collaborations=True)
    unshared = FileFull(id="2", owned_by=_OWNER, has_collaborations=False)

    shared_access = resolve_box_file_access(
        cast(BoxClient, fake), shared, folder_access, _ENTERPRISE
    )
    unshared_access = resolve_box_file_access(
        cast(BoxClient, fake), unshared, folder_access, _ENTERPRISE
    )

    assert shared_access is not None and unshared_access is not None
    assert shared_access.external_user_emails == {
        "owner@example.com",
        "dave@example.com",
    }
    assert unshared_access.external_user_emails == {"owner@example.com"}
    assert fake.list_collaborations.file_collaboration_calls == ["1"]


def test_web_link_collaborators_link_adds_no_one() -> None:
    web_link = WebLink(
        id="9",
        owned_by=_OWNER,
        shared_link=WebLinkSharedLinkField(
            url="https://app.box.com/s/abc",
            effective_access=WebLinkSharedLinkEffectiveAccessField.COLLABORATORS,
            effective_permission=WebLinkSharedLinkEffectivePermissionField.CAN_PREVIEW,
            is_password_enabled=False,
            download_count=0,
            preview_count=0,
        ),
    )

    access = resolve_box_web_link_access(web_link, ExternalAccess.empty(), _ENTERPRISE)

    assert access == ExternalAccess(
        external_user_emails={"owner@example.com"},
        external_user_group_ids=set(),
        is_public=False,
    )


def test_slim_perm_sync_cascades_folder_collaborators_to_files() -> None:
    modified = datetime(2024, 6, 1, tzinfo=timezone.utc)
    fake = _client(
        folders_by_id={
            "100": FolderFull(id="100", name="Root", owned_by=_OWNER),
            "200": FolderFull(id="200", name="Sub"),
        },
        pages={
            ("100", None): Items(
                entries=[FolderMini(id="200", name="Sub")], next_marker=None
            ),
            ("200", None): Items(
                entries=[
                    FileFull(
                        id="3",
                        name="b.txt",
                        size=10,
                        modified_at=modified,
                        has_collaborations=False,
                    )
                ],
                next_marker=None,
            ),
        },
        folder_collaborations={"100": [_group_collab("77")]},
    )
    connector = BoxConnector(folder_ids=["100"])
    connector._content_client = cast(BoxClient, fake)
    connector._enterprise_client = cast(BoxClient, fake)
    connector._enterprise_id = _ENTERPRISE

    docs: list[SlimDocument] = []
    nodes: list[HierarchyNode] = []
    for batch in connector.retrieve_all_slim_docs_perm_sync():
        for item in batch:
            if isinstance(item, SlimDocument):
                docs.append(item)
            else:
                nodes.append(item)

    assert [doc.id for doc in docs] == ["box-file-3"]
    expected = ExternalAccess(
        external_user_emails={"owner@example.com"},
        external_user_group_ids={"box_box-group-77"},
        is_public=False,
    )
    assert docs[0].external_access == expected
    assert all(node.external_access == expected for node in nodes)


def test_group_sync_lists_groups_members_and_all_users() -> None:
    fake = _client(
        groups=[GroupFull(id="g1"), GroupFull(id="g2"), GroupFull(id="g3")],
        members_by_group={
            "g1": [
                UserMini(id="a", login="A@example.com"),
                UserMini(id="b", login="b@example.com"),
                UserMini(id="c", login="c@example.com"),
            ],
            "g3": [UserMini(id="a", login="a@example.com")],
        },
        users_by_login={"A@example.com": "a", "z@example.com": "z"},
    )

    groups = {
        group.id: group.user_emails
        for group in iter_box_groups(cast(BoxClient, fake), _ENTERPRISE)
    }

    assert groups == {
        "box-group-g1": ["a@example.com", "b@example.com", "c@example.com"],
        "box-group-g2": [],
        "box-group-g3": ["a@example.com"],
        "box-enterprise-all-users-ent": ["a@example.com", "z@example.com"],
    }
    # The stored form matches the ids the resolver writes on documents.
    assert _group("box-group-g1") == "box_box-group-g1"
