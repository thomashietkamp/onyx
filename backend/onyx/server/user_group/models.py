"""User group API shapes. They match `UserGroup` and the request bodies in
`web/src/lib/types.ts` and `web/src/views/admin/GroupsPage/svc.ts`."""

from uuid import UUID

from pydantic import BaseModel, Field

from onyx.configs.app_configs import MASK_CREDENTIAL_PREFIX
from onyx.db.enums import AccountType, ConnectorManageRole
from onyx.db.models import User, UserGroup
from onyx.server.documents.models import (
    ConnectorCredentialPairDescriptor,
    ConnectorSnapshot,
    CredentialSnapshot,
)
from onyx.server.features.document_set.models import DocumentSetSummary
from onyx.server.features.persona.models import MinimalPersonaSnapshot


class GroupMember(BaseModel):
    id: UUID
    email: str
    is_active: bool
    account_type: AccountType
    personal_name: str | None

    @classmethod
    def from_model(cls, user: User) -> "GroupMember":
        return cls(
            id=user.id,
            email=user.email,
            is_active=user.is_active,
            account_type=user.account_type,
            personal_name=user.personal_name,
        )


class UserGroupSnapshot(BaseModel):
    id: int
    name: str
    # Empty when the list is fetched with include_users=false.
    users: list[GroupMember]
    member_count: int
    active_member_count: int
    manager_ids: list[UUID]
    cc_pairs: list[ConnectorCredentialPairDescriptor]
    document_sets: list[DocumentSetSummary]
    personas: list[MinimalPersonaSnapshot]
    is_up_to_date: bool
    is_up_for_deletion: bool
    is_default: bool
    incognito_enabled: bool
    # Affordance map for the caller (onyx/auth/permission_projection.py).
    permissions: dict[str, bool] = Field(default_factory=dict)

    @classmethod
    def from_model(
        cls,
        group: UserGroup,
        *,
        include_users: bool,
        member_count: int,
        active_member_count: int,
        manager_ids: list[UUID],
        permissions: dict[str, bool],
    ) -> "UserGroupSnapshot":
        current_cc_pair_ids = {
            row.cc_pair_id for row in group.cc_pair_relationships if row.is_current
        }
        return cls(
            id=group.id,
            name=group.name,
            users=(
                [GroupMember.from_model(user) for user in group.users]
                if include_users
                else []
            ),
            member_count=member_count,
            active_member_count=active_member_count,
            manager_ids=manager_ids,
            cc_pairs=[
                ConnectorCredentialPairDescriptor(
                    id=cc_pair.id,
                    name=cc_pair.name,
                    connector=ConnectorSnapshot.from_connector_db_model(
                        cc_pair.connector, credential_ids=[cc_pair.credential_id]
                    ),
                    credential=CredentialSnapshot.from_credential_db_model(
                        cc_pair.credential,
                        mask_credential_prefix=MASK_CREDENTIAL_PREFIX,
                    ),
                    access_type=cc_pair.access_type,
                )
                for cc_pair in group.cc_pairs
                if cc_pair.id in current_cc_pair_ids
            ],
            document_sets=[
                DocumentSetSummary.from_model(document_set)
                for document_set in group.document_sets
            ],
            personas=[
                MinimalPersonaSnapshot.from_model(persona)
                for persona in group.personas
                if not persona.deleted
            ],
            is_up_to_date=group.is_up_to_date,
            is_up_for_deletion=group.is_up_for_deletion,
            is_default=group.is_default,
            incognito_enabled=group.incognito_enabled,
            permissions=permissions,
        )


class MinimalUserGroupSnapshot(BaseModel):
    id: int
    name: str


class UserGroupCreate(BaseModel):
    name: str
    user_ids: list[UUID] = Field(default_factory=list)
    cc_pair_ids: list[int] = Field(default_factory=list)


class UserGroupUpdate(BaseModel):
    user_ids: list[UUID]
    # None leaves the group's connectors as they are.
    cc_pair_ids: list[int] | None = None


class UserGroupRename(BaseModel):
    id: int
    name: str


class AddUsersRequest(BaseModel):
    user_ids: list[UUID]


class SetManagerRequest(BaseModel):
    user_id: UUID
    is_manager: bool


class SetPermissionsRequest(BaseModel):
    permissions: list[str]


class ManagedCCPair(BaseModel):
    cc_pair_id: int
    role: ConnectorManageRole


class SetManagedCCPairsRequest(BaseModel):
    cc_pairs: list[ManagedCCPair]


class SetIncognitoRequest(BaseModel):
    enabled: bool
