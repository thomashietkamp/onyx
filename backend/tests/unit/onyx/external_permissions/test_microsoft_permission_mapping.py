"""The Microsoft ACL mappers and the Entra group expansion."""

from onyx.configs.constants import DocumentSource
from onyx.connectors.microsoft_utils.drive_items import DriveItemData
from onyx.connectors.microsoft_utils.entra import (
    EntraDirectoryObject,
    EntraGroup,
    EntraPage,
)
from onyx.connectors.microsoft_utils.models import (
    EntraMember,
    SharepointPrincipal,
    SharepointRoleAssignment,
    SharepointSecurable,
)
from onyx.connectors.microsoft_utils.sharepoint_access import SharepointPermissionCache
from onyx.connectors.microsoft_utils.sharepoint_principals import (
    SharepointPrincipalType,
)
from onyx.connectors.microsoft_utils.sharepoint_rest import SharepointPermissionReader
from onyx.connectors.onedrive.models import OneDrivePermission
from onyx.db.enums import ConnectorCredentialPairStatus
from onyx.db.external_perm import prefixed_group_id
from onyx.external_permissions.microsoft.group_sync import iter_entra_groups
from onyx.external_permissions.microsoft_utils.sharepoint_permissions import (
    get_external_access_from_sharepoint,
)
from onyx.external_permissions.onedrive.permission_mapper import (
    map_onedrive_permissions,
)
from onyx.external_permissions.sync_params import (
    doc_permission_sync_seconds,
    external_group_sync_seconds,
)

_GROUP_GUID = "11111111-2222-3333-4444-555555555555"


def _permission(payload: dict) -> OneDrivePermission:
    return OneDrivePermission.model_validate(payload)


def test_onedrive_grants_map_to_emails_and_groups() -> None:
    access = map_onedrive_permissions(
        [
            _permission(
                {"roles": ["read"], "grantedToV2": {"user": {"email": "Ann@x.com"}}}
            ),
            _permission(
                {"roles": ["write"], "grantedToV2": {"group": {"id": "GROUP-A"}}}
            ),
            # A pending invitation grants nothing.
            _permission(
                {"roles": ["sp.invite"], "grantedToV2": {"user": {"email": "z@x.com"}}}
            ),
        ],
        owner_email="Owner@x.com",
        treat_organization_link_as_public=False,
        add_prefix=True,
    )
    assert access.external_user_emails == {"owner@x.com", "ann@x.com"}
    assert access.external_user_group_ids == {
        prefixed_group_id("group-a", DocumentSource.ONEDRIVE)
    }
    assert not access.is_public


def test_onedrive_links_decide_public() -> None:
    org_link = _permission({"roles": ["read"], "link": {"scope": "organization"}})
    anonymous_link = _permission({"roles": ["read"], "link": {"scope": "anonymous"}})

    def is_public(links: list[OneDrivePermission], org_is_public: bool) -> bool:
        return map_onedrive_permissions(
            links, "o@x.com", org_is_public, add_prefix=False
        ).is_public

    assert not is_public([org_link], org_is_public=False)
    assert is_public([org_link], org_is_public=True)
    assert is_public([anonymous_link], org_is_public=False)


def test_entra_groups_expand_nested_members_once_and_stop_on_cycles() -> None:
    members: dict[str, list[EntraDirectoryObject]] = {
        "a": [
            EntraDirectoryObject(id="u1", mail="One@x.com"),
            EntraDirectoryObject(id="b", **{"@odata.type": "#microsoft.graph.group"}),
        ],
        "b": [
            EntraDirectoryObject(id="u2", userPrincipalName="two@x.com"),
            EntraDirectoryObject(id="a", **{"@odata.type": "#microsoft.graph.group"}),
        ],
    }
    reads: list[str] = []

    def list_groups(link: str | None) -> EntraPage[EntraGroup]:
        if link is None:
            return EntraPage(items=[EntraGroup(id="a")], next_link="page2")
        return EntraPage(items=[EntraGroup(id="b")])

    def list_members(
        group_id: str, link: str | None
    ) -> EntraPage[EntraDirectoryObject]:
        if link is not None:
            raise AssertionError("one page per group")
        reads.append(group_id)
        return EntraPage(items=members[group_id])

    groups = list(
        iter_entra_groups(list_groups, list_members, members_are_transitive=False)
    )

    assert {group.id: group.user_emails for group in groups} == {
        "a": ["one@x.com", "two@x.com"],
        "b": ["one@x.com", "two@x.com"],
    }
    assert sorted(reads) == ["a", "b"]


class _FakeSharepointReader(SharepointPermissionReader):
    def __init__(self, assignments: list[SharepointRoleAssignment]) -> None:
        self.assignments = assignments
        self.site_group_reads = 0

    def find_entra_group_id(self, *, display_name: str) -> str | None:
        return _GROUP_GUID if display_name == "Finance" else None

    def list_entra_group_members(self, *, group_id: str) -> list[EntraMember]:
        raise AssertionError(f"group sync expands {group_id}, not the mapper")

    def list_nested_entra_groups(
        self,
        *,
        group_id: str,
        max_rows: int | None = None,  # noqa: ARG002
    ) -> list[EntraGroup]:
        raise AssertionError(f"group sync expands {group_id}, not the mapper")

    def list_role_assignments(
        self,
        *,
        site_url: str,  # noqa: ARG002
        securable: SharepointSecurable,  # noqa: ARG002
        max_rows: int | None = None,  # noqa: ARG002
    ) -> list[SharepointRoleAssignment]:
        return self.assignments

    def list_site_group_users(
        self,
        *,
        site_url: str,  # noqa: ARG002
        group_name: str,
        max_rows: int | None = None,  # noqa: ARG002
    ) -> list[SharepointPrincipal]:
        self.site_group_reads += 1
        if group_name != "Site Members":
            return []
        return [
            SharepointPrincipal(
                principal_type=SharepointPrincipalType.USER,
                login_name="i:0#.f|membership|bob@x.com",
                title="Bob",
                user_principal_name="Bob@x.com",
            ),
            SharepointPrincipal(
                principal_type=SharepointPrincipalType.ENTRA_GROUP,
                login_name="Finance",
                title="Finance",
            ),
        ]

    def get_folder_unique_id(self, *, site_url: str, server_relative_path: str) -> str:
        return f"{site_url}{server_relative_path}"

    def get_list_item_id(self, *, item: DriveItemData) -> int | None:  # noqa: ARG002
        return None

    def list_sharing_link_scopes(
        self,
        *,
        item: DriveItemData,  # noqa: ARG002
        max_rows: int | None = None,  # noqa: ARG002
    ) -> list[str]:
        return []


def _assignment(
    principal: SharepointPrincipal, kinds: list[int]
) -> SharepointRoleAssignment:
    return SharepointRoleAssignment(member=principal, role_type_kinds=kinds)


def test_sharepoint_site_access_expands_site_groups_and_skips_limited_access() -> None:
    reader = _FakeSharepointReader(
        [
            _assignment(
                SharepointPrincipal(
                    principal_type=SharepointPrincipalType.USER,
                    login_name="i:0#.f|membership|ann@x.com",
                    title="Ann",
                ),
                [2],
            ),
            _assignment(
                SharepointPrincipal(
                    principal_type=SharepointPrincipalType.SHAREPOINT_GROUP,
                    login_name="Site Members",
                    title="Site Members",
                ),
                [3],
            ),
            # Limited Access only: no read.
            _assignment(
                SharepointPrincipal(
                    principal_type=SharepointPrincipalType.USER,
                    login_name="i:0#.f|membership|limited@x.com",
                    title="Limited",
                ),
                [1],
            ),
        ]
    )
    cache = SharepointPermissionCache()

    def site_access() -> tuple[set[str], set[str], bool]:
        access = get_external_access_from_sharepoint(
            reader,
            "https://tenant.sharepoint.com/sites/a",
            list_id=None,
            drive_item=None,
            site_page=None,
            add_prefix=True,
            treat_sharing_link_as_public=False,
            permission_cache=cache,
        )
        return (
            set(access.external_user_emails),
            set(access.external_user_group_ids),
            access.is_public,
        )

    emails, group_ids, is_public = site_access()
    assert emails == {"ann@x.com", "bob@x.com"}
    assert group_ids == {prefixed_group_id(_GROUP_GUID, DocumentSource.SHAREPOINT)}
    assert not is_public

    site_access()
    assert reader.site_group_reads == 1


def test_sharepoint_everyone_claim_makes_site_public() -> None:
    reader = _FakeSharepointReader(
        [
            _assignment(
                SharepointPrincipal(
                    principal_type=SharepointPrincipalType.ENTRA_GROUP,
                    login_name="c:0-.f|rolemanager|spo-grid-all-users/abc",
                    title="Everyone except external users",
                ),
                [2],
            )
        ]
    )
    access = get_external_access_from_sharepoint(
        reader,
        "https://tenant.sharepoint.com/sites/a",
        list_id=None,
        drive_item=None,
        site_page=None,
        add_prefix=True,
        treat_sharing_link_as_public=False,
        permission_cache=SharepointPermissionCache(),
    )
    assert access.is_public


def test_prefixed_group_id_is_idempotent() -> None:
    once = prefixed_group_id("Group-A", DocumentSource.SHAREPOINT)
    assert prefixed_group_id(once, DocumentSource.SHAREPOINT) == once


def test_sync_schedule_waits_for_first_index_and_skips_paused_pairs() -> None:
    active = ConnectorCredentialPairStatus.ACTIVE
    paused = ConnectorCredentialPairStatus.PAUSED
    assert doc_permission_sync_seconds(DocumentSource.SHAREPOINT, active, False) is None
    assert doc_permission_sync_seconds(DocumentSource.SHAREPOINT, active, True)
    assert doc_permission_sync_seconds(DocumentSource.SHAREPOINT, paused, True) is None
    assert doc_permission_sync_seconds(DocumentSource.WEB, active, True) is None
    assert external_group_sync_seconds(DocumentSource.OUTLOOK, active) is None
    assert external_group_sync_seconds(DocumentSource.TEAMS, active)
