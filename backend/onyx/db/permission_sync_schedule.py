"""Which perm-synced pairs exist, and when a pair that moved to Sync may show
its documents again."""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from onyx.db.document import build_cc_pair_has_unsynced_documents_clause
from onyx.db.enums import AccessType, ConnectorCredentialPairStatus
from onyx.db.models import ConnectorCredentialPair


def fetch_perm_synced_cc_pairs(db_session: Session) -> list[ConnectorCredentialPair]:
    """Live pairs whose access comes from the source."""
    return list(
        db_session.scalars(
            select(ConnectorCredentialPair).where(
                ConnectorCredentialPair.access_type.in_(AccessType.perm_synced_types()),
                ConnectorCredentialPair.status
                != ConnectorCredentialPairStatus.DELETING,
            )
        ).all()
    )


def clear_caught_up_perm_sync_pending_marks(db_session: Session) -> int:
    """A pair that just moved to Sync stays hidden until its chunks carry the
    synced ACLs: after a successful doc sync and once no document of the pair
    waits for the metadata sync."""
    result = db_session.execute(
        update(ConnectorCredentialPair)
        .where(
            ConnectorCredentialPair.perm_sync_pending_since.is_not(None),
            ConnectorCredentialPair.last_time_perm_sync.is_not(None),
            ConnectorCredentialPair.last_time_perm_sync
            >= ConnectorCredentialPair.perm_sync_pending_since,
            ~build_cc_pair_has_unsynced_documents_clause(),
        )
        .values(perm_sync_pending_since=None)
    )
    db_session.commit()
    return int(result.rowcount or 0)  # ty: ignore[unresolved-attribute]
