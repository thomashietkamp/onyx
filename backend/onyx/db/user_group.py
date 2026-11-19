"""Invariants for the seeded default groups (Admin/Basic).

A default group holds members and nothing else: no connectors, document sets, agents,
skills, providers or limits, and its name is fixed. Its permission grants come from
migrations, except the few an admin may switch on the Basic group
(``DEFAULT_GROUP_TOGGLEABLE_PERMISSIONS``).

The rest of the module is this fork's group management: create, edit, rename, delete,
managers and permission grants, and the document-sync hooks that
``fetch_versioned_implementation("onyx.db.user_group", ...)`` resolves to.
"""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import Select, and_, delete, select, union
from sqlalchemy.orm import Session

from onyx.auth.permissions import (
    DEFAULT_GROUP_TOGGLEABLE_PERMISSIONS,
)
from onyx.db.enums import (
    AccessType,
    ConnectorCredentialPairStatus,
    Permission,
)
from onyx.db.models import (
    ConnectorCredentialPair,
    Credential__UserGroup,
    DocumentByConnectorCredentialPair,
    DocumentSet__UserGroup,
    LLMProvider__UserGroup,
    MCPServer__UserGroup,
    Persona__UserGroup,
    User__UserGroup,
    UserGroup,
    UserGroup__CCPairDataAccess,
    UserGroup__ConnectorCredentialPair,
)
from onyx.db.users import (
    DEFAULT_BASIC_GROUP_NAME,
)
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError


def group_permission_toggles_allowed(user_group: UserGroup) -> bool:
    """Custom groups take any toggleable grant, Basic its allowlist, Admin nothing."""
    return not user_group.is_default or user_group.name == DEFAULT_BASIC_GROUP_NAME


def assert_group_config_is_editable(
    db_session: Session, user_group_id: int, action: str
) -> None:
    """A default group holds only members, so the config writes this guards are refused.
    ``action`` completes "Cannot {action} a default system group.". A missing group falls
    through to the caller's own NOT_FOUND."""
    assert_groups_config_are_editable(db_session, [user_group_id], action)


def assert_groups_config_are_editable(
    db_session: Session, user_group_ids: Collection[int], action: str
) -> None:
    """Batch form for a caller holding several ids — one query rather than one per id."""
    if not user_group_ids:
        return

    is_default_present = db_session.scalar(
        select(UserGroup.id)
        .where(
            UserGroup.id.in_(user_group_ids),
            UserGroup.is_default.is_(True),
        )
        .limit(1)
    )
    if is_default_present is not None:
        raise OnyxError(
            OnyxErrorCode.CONFLICT, f"Cannot {action} a default system group."
        )


def assert_not_shared_with_default_group(
    db_session: Session, group_ids: Collection[int]
) -> None:
    """Keep a default group empty of everything but members, from any resource's own side.
    Basic holds the whole org and Admin every admin, so sharing with one is really "make
    this public", which every resource already expresses with its own public flag."""
    if not group_ids:
        return

    default_names = db_session.scalars(
        select(UserGroup.name).where(
            UserGroup.id.in_(group_ids),
            UserGroup.is_default.is_(True),
        )
    ).all()
    if default_names:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            "Cannot share with the default system group(s): "
            + ", ".join(sorted(default_names))
            + ". Make the resource public instead.",
        )


def assert_group_permissions_editable(
    user_group: UserGroup, permissions: Collection[Permission]
) -> None:
    """A custom group takes any toggleable grant. Basic takes only the allowlisted
    ones, since it holds the whole org, and Admin takes none."""
    if not user_group.is_default:
        return
    if not group_permission_toggles_allowed(user_group):
        raise OnyxError(
            OnyxErrorCode.CONFLICT,
            "Cannot change the permissions of a default system group.",
        )
    refused: list[str] = sorted(
        p.value for p in permissions if p not in DEFAULT_GROUP_TOGGLEABLE_PERMISSIONS
    )
    if refused:
        raise OnyxError(
            OnyxErrorCode.CONFLICT,
            f"Permissions {refused} cannot be toggled on the Basic group.",
        )


# ---------------------------------------------------------------------------
# Group sync hooks (dispatched by name from the document sync)
# ---------------------------------------------------------------------------
_UNCASCADED_LINK_TABLES = (
    Persona__UserGroup,
    LLMProvider__UserGroup,
    DocumentSet__UserGroup,
    Credential__UserGroup,
    MCPServer__UserGroup,
)


# Accounts that take part in the group system (see AccountType).
def fetch_user_groups(
    db_session: Session,
    only_up_to_date: bool = True,
    include_default: bool = True,
    group_ids: Collection[int] | None = None,
) -> list[UserGroup]:
    """Groups by name. The document sync calls it with ``only_up_to_date=False``."""
    stmt = select(UserGroup).order_by(UserGroup.name)
    if only_up_to_date:
        stmt = stmt.where(UserGroup.is_up_to_date.is_(True))
    if not include_default:
        stmt = stmt.where(UserGroup.is_default.is_(False))
    if group_ids is not None:
        stmt = stmt.where(UserGroup.id.in_(group_ids))
    return list(db_session.scalars(stmt).unique().all())


def fetch_user_group(db_session: Session, user_group_id: int) -> UserGroup | None:
    """Re-reads the row, so a sync loop never acts on a cached state."""
    return db_session.scalar(
        select(UserGroup)
        .where(UserGroup.id == user_group_id)
        .execution_options(populate_existing=True)
    )


def delete_user_group__no_commit(db_session: Session, group: UserGroup) -> None:
    """Removes the group and every row that points at it."""
    for table in _UNCASCADED_LINK_TABLES:
        db_session.execute(delete(table).where(table.user_group_id == group.id))
    db_session.execute(
        delete(UserGroup__ConnectorCredentialPair).where(
            UserGroup__ConnectorCredentialPair.user_group_id == group.id
        )
    )
    db_session.execute(
        delete(User__UserGroup).where(User__UserGroup.user_group_id == group.id)
    )
    db_session.delete(group)
    db_session.flush()


def mark_user_group_as_synced__no_commit(db_session: Session, group: UserGroup) -> None:
    db_session.execute(
        delete(UserGroup__ConnectorCredentialPair).where(
            UserGroup__ConnectorCredentialPair.user_group_id == group.id,
            UserGroup__ConnectorCredentialPair.is_current.is_(False),
        )
    )
    group.is_up_to_date = True
    db_session.flush()


def construct_document_id_select_by_usergroup(user_group_id: int) -> Select:
    """Documents whose ACL may change with this group: those on any cc-pair the
    group manages now or did before the last edit, or gives data access to."""
    linked_cc_pair_ids = union(
        select(UserGroup__ConnectorCredentialPair.cc_pair_id).where(
            UserGroup__ConnectorCredentialPair.user_group_id == user_group_id
        ),
        select(UserGroup__CCPairDataAccess.cc_pair_id).where(
            UserGroup__CCPairDataAccess.user_group_id == user_group_id
        ),
    ).subquery()
    return (
        select(DocumentByConnectorCredentialPair.id)
        .join(
            ConnectorCredentialPair,
            and_(
                ConnectorCredentialPair.connector_id
                == DocumentByConnectorCredentialPair.connector_id,
                ConnectorCredentialPair.credential_id
                == DocumentByConnectorCredentialPair.credential_id,
            ),
        )
        .where(ConnectorCredentialPair.id.in_(select(linked_cc_pair_ids.c.cc_pair_id)))
        .distinct()
        .order_by(DocumentByConnectorCredentialPair.id)
    )


def delete_user_group_cc_pair_relationship__no_commit(
    cc_pair_id: int, db_session: Session
) -> None:
    """Called when a connector is deleted. Data-access rows go with the pair."""
    db_session.execute(
        delete(UserGroup__ConnectorCredentialPair).where(
            UserGroup__ConnectorCredentialPair.cc_pair_id == cc_pair_id
        )
    )


# ---------------------------------------------------------------------------
# Document ACLs
# ---------------------------------------------------------------------------


def fetch_user_group_names_for_documents(
    db_session: Session, document_ids: Collection[str]
) -> dict[str, set[str]]:
    """The ``group:`` ACL entries of each document, through its live cc-pairs:
    the data-access groups of a PRIVATE pair, and the manage groups of a PUBLIC
    pair (public already admits everyone; the name only records the link).
    Manage rows on a PRIVATE pair grant no document access. A SYNC_RESTRICTED
    pair's groups only open the pair at query time; its documents keep the
    source's own ACL."""
    names: dict[str, set[str]] = {}
    if not document_ids:
        return names

    doc_pair = and_(
        ConnectorCredentialPair.connector_id
        == DocumentByConnectorCredentialPair.connector_id,
        ConnectorCredentialPair.credential_id
        == DocumentByConnectorCredentialPair.credential_id,
    )
    live = and_(
        DocumentByConnectorCredentialPair.id.in_(document_ids),
        ConnectorCredentialPair.status != ConnectorCredentialPairStatus.DELETING,
        UserGroup.is_up_for_deletion.is_(False),
    )
    data_access = (
        select(DocumentByConnectorCredentialPair.id, UserGroup.name)
        .join(ConnectorCredentialPair, doc_pair)
        .join(
            UserGroup__CCPairDataAccess,
            UserGroup__CCPairDataAccess.cc_pair_id == ConnectorCredentialPair.id,
        )
        .join(UserGroup, UserGroup.id == UserGroup__CCPairDataAccess.user_group_id)
        .where(live, ConnectorCredentialPair.access_type == AccessType.PRIVATE)
    )
    public_links = (
        select(DocumentByConnectorCredentialPair.id, UserGroup.name)
        .join(ConnectorCredentialPair, doc_pair)
        .join(
            UserGroup__ConnectorCredentialPair,
            and_(
                UserGroup__ConnectorCredentialPair.cc_pair_id
                == ConnectorCredentialPair.id,
                UserGroup__ConnectorCredentialPair.is_current.is_(True),
            ),
        )
        .join(
            UserGroup, UserGroup.id == UserGroup__ConnectorCredentialPair.user_group_id
        )
        .where(live, ConnectorCredentialPair.access_type == AccessType.PUBLIC)
    )
    for document_id, name in db_session.execute(union(data_access, public_links)):
        names.setdefault(document_id, set()).add(name)
    return names


def fetch_user_group_names_for_user(db_session: Session, user_id: UUID) -> set[str]:
    """The ``group:`` ACL entries a user matches. Default groups never hold
    connectors, and a group up for deletion grants nothing."""
    return set(
        db_session.scalars(
            select(UserGroup.name)
            .join(User__UserGroup, User__UserGroup.user_group_id == UserGroup.id)
            .where(
                User__UserGroup.user_id == user_id,
                UserGroup.is_default.is_(False),
                UserGroup.is_up_for_deletion.is_(False),
            )
        ).all()
    )
