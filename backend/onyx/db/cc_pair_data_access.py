"""Data-access groups of cc-pairs: the groups whose members may read a pair's
documents. Only PRIVATE and SYNC_RESTRICTED pairs have them (the table's CHECK
constraint). A change rewrites the ``group:`` entries of the pair's chunks, so
every write here marks the pair's documents for sync.

The connector edit flow reaches these functions through
``fetch_ee_implementation_or_noop("onyx.db.cc_pair_data_access", ...)``.
"""

from collections.abc import Collection

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from onyx.db.document import mark_cc_pair_documents_for_sync__no_commit
from onyx.db.enums import AccessType
from onyx.db.models import (
    ConnectorCredentialPair,
    UserGroup,
    UserGroup__CCPairDataAccess,
)
from onyx.db.user_group import assert_not_shared_with_default_group
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError


def fetch_data_access_group_ids(db_session: Session, cc_pair_id: int) -> set[int]:
    return set(
        db_session.scalars(
            select(UserGroup__CCPairDataAccess.user_group_id).where(
                UserGroup__CCPairDataAccess.cc_pair_id == cc_pair_id
            )
        ).all()
    )


def fetch_data_access_groups_for_cc_pair(
    db_session: Session, cc_pair_id: int
) -> list[UserGroup]:
    return list(
        db_session.scalars(
            select(UserGroup)
            .join(
                UserGroup__CCPairDataAccess,
                UserGroup__CCPairDataAccess.user_group_id == UserGroup.id,
            )
            .where(UserGroup__CCPairDataAccess.cc_pair_id == cc_pair_id)
            .order_by(UserGroup.name)
        )
        .unique()
        .all()
    )


def fetch_data_access_cc_pair_ids_for_group(
    db_session: Session, group_id: int
) -> set[int]:
    return set(
        db_session.scalars(
            select(UserGroup__CCPairDataAccess.cc_pair_id).where(
                UserGroup__CCPairDataAccess.user_group_id == group_id
            )
        ).all()
    )


def _assert_groups_usable(db_session: Session, group_ids: Collection[int]) -> None:
    if not group_ids:
        return
    assert_not_shared_with_default_group(db_session, group_ids)
    found = set(
        db_session.scalars(
            select(UserGroup.id).where(
                UserGroup.id.in_(group_ids), UserGroup.is_up_for_deletion.is_(False)
            )
        ).all()
    )
    if missing := set(group_ids) - found:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT, f"User group(s) not found: {sorted(missing)}"
        )


def set_cc_pair_data_access_groups__no_commit(
    db_session: Session,
    *,
    cc_pair_id: int,
    requested_group_ids: Collection[int],
    visible_group_ids: Collection[int] | None,
) -> set[int]:
    """Makes the pair's data-access groups ``requested_group_ids``, limited to the
    groups the caller can see (``visible_group_ids``; None means all). Groups the
    caller cannot see stay as they are, and adding one is refused. Returns the
    final set."""
    cc_pair = db_session.scalar(
        select(ConnectorCredentialPair).where(ConnectorCredentialPair.id == cc_pair_id)
    )
    if cc_pair is None:
        raise OnyxError(
            OnyxErrorCode.CONNECTOR_NOT_FOUND, f"Connector {cc_pair_id} not found."
        )
    current = fetch_data_access_group_ids(db_session, cc_pair_id)
    requested = set(requested_group_ids)
    if visible_group_ids is not None:
        visible = set(visible_group_ids)
        if hidden_added := requested - current - visible:
            raise OnyxError(
                OnyxErrorCode.INSUFFICIENT_PERMISSIONS,
                "Group managers can only act on private resources within the "
                f"groups they manage: {sorted(hidden_added)}",
            )
        requested |= current - visible

    if requested and cc_pair.access_type not in AccessType.data_access_types():
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            f"A {cc_pair.access_type.value} connector has no data-access groups.",
        )
    _assert_groups_usable(db_session, requested - current)

    if requested == current:
        return current
    db_session.execute(
        delete(UserGroup__CCPairDataAccess).where(
            UserGroup__CCPairDataAccess.cc_pair_id == cc_pair_id,
            UserGroup__CCPairDataAccess.user_group_id.in_(current - requested),
        )
    )
    for group_id in requested - current:
        db_session.add(
            UserGroup__CCPairDataAccess(
                cc_pair_id=cc_pair_id,
                user_group_id=group_id,
                access_type=cc_pair.access_type,
            )
        )
    db_session.flush()
    mark_cc_pair_documents_for_sync__no_commit(db_session, [cc_pair_id])
    return requested


def assert_restricted_cc_pairs_keep_a_group(
    db_session: Session, cc_pair_ids: Collection[int]
) -> None:
    """A SYNC_RESTRICTED pair with no data-access group is visible to nobody."""
    if not cc_pair_ids:
        return
    restricted = set(
        db_session.scalars(
            select(ConnectorCredentialPair.id).where(
                ConnectorCredentialPair.id.in_(cc_pair_ids),
                ConnectorCredentialPair.access_type == AccessType.SYNC_RESTRICTED,
            )
        ).all()
    )
    if not restricted:
        return
    with_groups = set(
        db_session.scalars(
            select(UserGroup__CCPairDataAccess.cc_pair_id).where(
                UserGroup__CCPairDataAccess.cc_pair_id.in_(restricted)
            )
        ).all()
    )
    if groupless := restricted - with_groups:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            "A group-restricted connector needs at least one data-access group: "
            f"{sorted(groupless)}",
        )
