"""Who may read a Box item. The Box connector reaches this through
``onyx/connectors/box/access.py``.

Box access flows down the folder tree: the owner and the accepted
collaborators (users and groups) of a folder can read everything below it, and
so can everyone a folder's shared link reaches. An item's access is therefore
the union over the item and every folder above it. An open shared link makes
the item public; a company link adds the enterprise's all-users group; a
collaborators-only link adds no one. The root folder ``0`` has no
collaborations (Box rejects the query), only its owner.

Group ids are returned with the Onyx source prefix, because indexing and
hierarchy nodes store them as given.
"""

from enum import Enum

from box_sdk_gen import BoxClient
from box_sdk_gen.schemas.collaboration import Collaboration, CollaborationStatusField
from box_sdk_gen.schemas.file_full import FileFull
from box_sdk_gen.schemas.folder_mini import FolderMini
from box_sdk_gen.schemas.group_mini import GroupMini, GroupMiniGroupTypeField
from box_sdk_gen.schemas.user_collaborations import UserCollaborations
from box_sdk_gen.schemas.user_mini import UserMini
from box_sdk_gen.schemas.web_link import WebLink
from pydantic import BaseModel, Field

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.box.connector import (
    BOX_ROOT_FOLDER_ID,
    box_all_enterprise_users_group_id,
    box_group_id,
    normalize_box_login,
)

_COLLABORATIONS_PAGE_SIZE = 1000
_MAX_PAGES = 10_000
_FOLDER_FIELDS = ["owned_by", "shared_link"]
_OPEN_LINK = "open"
_COMPANY_LINK = "company"


class _AccessBuilder(BaseModel):
    emails: set[str] = Field(default_factory=set)
    group_ids: set[str] = Field(default_factory=set)
    is_public: bool = False

    def add_access(self, access: ExternalAccess | None) -> None:
        if access is None:
            return
        self.emails |= access.external_user_emails
        self.group_ids |= access.external_user_group_ids
        self.is_public = self.is_public or access.is_public

    def add_owner(self, owner: UserMini | None) -> None:
        if owner is not None and owner.login:
            self.emails.add(normalize_box_login(owner.login))

    def add_shared_link(
        self, effective_access: Enum | None, enterprise_id: str
    ) -> None:
        if effective_access is None:
            return
        if effective_access.value == _OPEN_LINK:
            self.is_public = True
        elif effective_access.value == _COMPANY_LINK:
            self.group_ids.add(
                _onyx_group_id(box_all_enterprise_users_group_id(enterprise_id))
            )

    def add_collaborations(
        self, collaborations: list[Collaboration], enterprise_id: str
    ) -> None:
        for collaboration in collaborations:
            if collaboration.status not in (None, CollaborationStatusField.ACCEPTED):
                continue
            grantee = collaboration.accessible_by
            if isinstance(grantee, GroupMini):
                if grantee.group_type == GroupMiniGroupTypeField.ALL_USERS_GROUP:
                    raw_id = box_all_enterprise_users_group_id(enterprise_id)
                else:
                    raw_id = box_group_id(grantee.id)
                self.group_ids.add(_onyx_group_id(raw_id))
            elif isinstance(grantee, UserCollaborations) and grantee.login:
                self.emails.add(normalize_box_login(grantee.login))

    def build(self) -> ExternalAccess:
        return ExternalAccess(
            external_user_emails=set(self.emails),
            external_user_group_ids=set(self.group_ids),
            is_public=self.is_public,
        )


def _onyx_group_id(raw_group_id: str) -> str:
    return build_ext_group_name_for_onyx(raw_group_id, DocumentSource.BOX)


def _list_folder_collaborations(
    client: BoxClient, folder_id: str
) -> list[Collaboration]:
    if folder_id == BOX_ROOT_FOLDER_ID:
        return []
    collaborations: list[Collaboration] = []
    marker: str | None = None
    for _ in range(_MAX_PAGES):
        page = client.list_collaborations.get_folder_collaborations(
            folder_id, limit=_COLLABORATIONS_PAGE_SIZE, marker=marker
        )
        collaborations.extend(page.entries or [])
        marker = page.next_marker
        if not marker:
            return collaborations
    raise RuntimeError(f"Box collaborations of folder {folder_id} did not terminate")


def _list_file_collaborations(client: BoxClient, file_id: str) -> list[Collaboration]:
    collaborations: list[Collaboration] = []
    marker: str | None = None
    for _ in range(_MAX_PAGES):
        page = client.list_collaborations.get_file_collaborations(
            file_id, limit=_COLLABORATIONS_PAGE_SIZE, marker=marker
        )
        collaborations.extend(page.entries or [])
        marker = page.next_marker
        if not marker:
            return collaborations
    raise RuntimeError(f"Box collaborations of file {file_id} did not terminate")


def _add_folder(
    builder: _AccessBuilder, client: BoxClient, folder_id: str, enterprise_id: str
) -> None:
    folder = client.folders.get_folder_by_id(folder_id, fields=_FOLDER_FIELDS)
    builder.add_owner(folder.owned_by)
    if folder.shared_link is not None:
        builder.add_shared_link(folder.shared_link.effective_access, enterprise_id)
    builder.add_collaborations(
        _list_folder_collaborations(client, folder_id), enterprise_id
    )


def resolve_box_ancestor_access(
    client: BoxClient,
    path_entries: list[FolderMini] | None,
    enterprise_id: str,
) -> ExternalAccess | None:
    """Access inherited from the folders above an entry folder."""
    builder = _AccessBuilder()
    for entry in path_entries or []:
        if entry.id == BOX_ROOT_FOLDER_ID:
            continue
        _add_folder(builder, client, entry.id, enterprise_id)
    return builder.build()


def resolve_box_folder_access(
    client: BoxClient,
    folder_id: str,
    inherited_access: ExternalAccess | None,
    enterprise_id: str,
) -> ExternalAccess | None:
    """The folder's own grants added to what it inherits."""
    builder = _AccessBuilder()
    builder.add_access(inherited_access)
    _add_folder(builder, client, folder_id, enterprise_id)
    return builder.build()


def resolve_box_file_access(
    client: BoxClient,
    file: FileFull,
    folder_access: ExternalAccess,
    enterprise_id: str,
) -> ExternalAccess | None:
    builder = _AccessBuilder()
    builder.add_access(folder_access)
    builder.add_owner(file.owned_by)
    if file.shared_link is not None:
        builder.add_shared_link(file.shared_link.effective_access, enterprise_id)
    # A listing without the field cannot rule collaborations out.
    if file.has_collaborations is not False:
        builder.add_collaborations(
            _list_file_collaborations(client, file.id), enterprise_id
        )
    return builder.build()


def resolve_box_web_link_access(
    web_link: WebLink,
    folder_access: ExternalAccess,
    enterprise_id: str,
) -> ExternalAccess | None:
    """Box has no collaborations on web links, only the owner and the link."""
    builder = _AccessBuilder()
    builder.add_access(folder_access)
    builder.add_owner(web_link.owned_by)
    if web_link.shared_link is not None:
        builder.add_shared_link(web_link.shared_link.effective_access, enterprise_id)
    return builder.build()
