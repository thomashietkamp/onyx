"""Synced permissions from source systems: the members of external groups, and
the synced ACL of each document.

External group ids are stored with their source prefix
(``onyx/access/utils.py:build_ext_group_name_for_onyx``), the same form as in
``Document.external_user_group_ids``, so the read side compares them directly.
A group sync marks its pair's rows stale first, upserts what the source returns
as current, then deletes what stayed stale: a failed sync keeps last cycle's
memberships rather than revoking everything.
"""

from collections.abc import Collection, Iterable
from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.db.enums import AccessType, ConnectorCredentialPairStatus
from onyx.db.models import (
    ConnectorCredentialPair,
    DocumentByConnectorCredentialPair,
    PublicExternalUserGroup,
    User__ExternalUserGroupId,
)
from onyx.db.models import Document as DbDocument
from onyx.db.users import batch_add_ext_perm_user_if_not_exists


class ExternalUserGroup(BaseModel):
    """A group as a source reports it. ``id`` is the source's own id, without
    the Onyx source prefix."""

    id: str
    user_emails: list[str] = Field(default_factory=list)
    # The source lets anyone in the organization read what this group reads.
    gives_anyone_access: bool = False


def prefixed_group_id(group_id: str, source: DocumentSource) -> str:
    """The stored form of a group id. Idempotent: connectors that already add
    the prefix while indexing pass through unchanged."""
    prefix = f"{source.value}_".lower()
    lowered = group_id.lower()
    return (
        lowered
        if lowered.startswith(prefix)
        else build_ext_group_name_for_onyx(group_id, source)
    )


# --- group sync ----------------------------------------------------------------


def mark_old_external_groups_as_stale(db_session: Session, cc_pair_id: int) -> None:
    db_session.execute(
        update(User__ExternalUserGroupId)
        .where(User__ExternalUserGroupId.cc_pair_id == cc_pair_id)
        .values(stale=True)
    )
    db_session.execute(
        update(PublicExternalUserGroup)
        .where(PublicExternalUserGroup.cc_pair_id == cc_pair_id)
        .values(stale=True)
    )
    db_session.commit()


def upsert_external_groups(
    db_session: Session,
    cc_pair_id: int,
    source: DocumentSource,
    groups: Iterable[ExternalUserGroup],
) -> int:
    """Writes one batch of groups as current. Unknown emails get an
    external-permission user, so access is ready when the person signs up.
    Returns the number of memberships written."""
    groups = list(groups)
    emails = sorted({email.lower() for group in groups for email in group.user_emails})
    users = batch_add_ext_perm_user_if_not_exists(
        db_session, emails, continue_on_error=True
    )
    user_id_by_email: dict[str, UUID] = {user.email.lower(): user.id for user in users}

    memberships: list[dict[str, object]] = []
    for group in groups:
        group_id = prefixed_group_id(group.id, source)
        for email in {e.lower() for e in group.user_emails}:
            user_id = user_id_by_email.get(email)
            if user_id is not None:
                memberships.append(
                    {
                        "user_id": user_id,
                        "external_user_group_id": group_id,
                        "cc_pair_id": cc_pair_id,
                        "stale": False,
                    }
                )
        if group.gives_anyone_access:
            db_session.execute(
                pg_insert(PublicExternalUserGroup)
                .values(
                    external_user_group_id=group_id,
                    cc_pair_id=cc_pair_id,
                    stale=False,
                )
                .on_conflict_do_update(
                    index_elements=["external_user_group_id", "cc_pair_id"],
                    set_={"stale": False},
                )
            )
    if memberships:
        statement = pg_insert(User__ExternalUserGroupId).values(memberships)
        db_session.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id", "external_user_group_id", "cc_pair_id"],
                set_={"stale": False},
            )
        )
    db_session.commit()
    return len(memberships)


def remove_stale_external_groups(db_session: Session, cc_pair_id: int) -> None:
    db_session.execute(
        delete(User__ExternalUserGroupId).where(
            User__ExternalUserGroupId.cc_pair_id == cc_pair_id,
            User__ExternalUserGroupId.stale.is_(True),
        )
    )
    db_session.execute(
        delete(PublicExternalUserGroup).where(
            PublicExternalUserGroup.cc_pair_id == cc_pair_id,
            PublicExternalUserGroup.stale.is_(True),
        )
    )
    db_session.commit()


def delete_user__ext_group_for_user__no_commit(
    db_session: Session, user_id: UUID
) -> None:
    db_session.execute(
        delete(User__ExternalUserGroupId).where(
            User__ExternalUserGroupId.user_id == user_id
        )
    )


def delete_user__ext_group_for_cc_pair__no_commit(
    db_session: Session, cc_pair_id: int
) -> None:
    db_session.execute(
        delete(User__ExternalUserGroupId).where(
            User__ExternalUserGroupId.cc_pair_id == cc_pair_id
        )
    )
    db_session.execute(
        delete(PublicExternalUserGroup).where(
            PublicExternalUserGroup.cc_pair_id == cc_pair_id
        )
    )


def fetch_external_group_ids_for_user(db_session: Session, user_id: UUID) -> set[str]:
    """The user's external groups, plus every group that admits anyone."""
    member_of = set(
        db_session.scalars(
            select(User__ExternalUserGroupId.external_user_group_id).where(
                User__ExternalUserGroupId.user_id == user_id
            )
        ).all()
    )
    public = set(
        db_session.scalars(
            select(PublicExternalUserGroup.external_user_group_id).distinct()
        ).all()
    )
    return member_of | public


# --- doc sync ------------------------------------------------------------------


def upsert_document_external_perms(
    db_session: Session,
    document_id: str,
    external_access: ExternalAccess,
    source: DocumentSource,
) -> bool:
    """Writes the document's synced ACL. Bumps ``last_modified`` only when the
    value changed, so the metadata sync rewrites just those chunks. Returns
    whether it changed; an unknown document is skipped (indexing creates it)."""
    document = db_session.get(DbDocument, document_id)
    if document is None:
        return False
    emails = sorted({email.lower() for email in external_access.external_user_emails})
    group_ids = sorted(
        {prefixed_group_id(g, source) for g in external_access.external_user_group_ids}
    )
    if (
        sorted(document.external_user_emails or []) == emails
        and sorted(document.external_user_group_ids or []) == group_ids
        and bool(document.is_public) == external_access.is_public
    ):
        return False
    document.external_user_emails = emails
    document.external_user_group_ids = group_ids
    document.is_public = external_access.is_public
    document.last_modified = datetime.now(timezone.utc)
    return True


def fetch_synced_access_for_documents(
    db_session: Session, document_ids: Collection[str]
) -> dict[str, tuple[list[str], list[str], bool]]:
    """document id -> (emails, prefixed group ids, is_public) from the synced
    columns, for documents on at least one live perm-synced pair. Documents
    only on other pairs are absent: their synced columns grant nothing."""
    if not document_ids:
        return {}
    synced_doc_ids = (
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
        .where(
            DocumentByConnectorCredentialPair.id.in_(document_ids),
            ConnectorCredentialPair.access_type.in_(AccessType.perm_synced_types()),
            ConnectorCredentialPair.status != ConnectorCredentialPairStatus.DELETING,
        )
    )
    rows = db_session.execute(
        select(
            DbDocument.id,
            DbDocument.external_user_emails,
            DbDocument.external_user_group_ids,
            DbDocument.is_public,
        ).where(DbDocument.id.in_(synced_doc_ids))
    ).all()
    return {
        doc_id: (list(emails or []), list(group_ids or []), bool(is_public))
        for doc_id, emails, group_ids, is_public in rows
    }
