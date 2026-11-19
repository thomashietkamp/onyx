"""Reads and resolves the users and groups a group sync skipped."""

from collections.abc import Collection

from sqlalchemy import ColumnElement, Select, case, func, select
from sqlalchemy.orm import Session

from onyx.db.cc_pair_run_resolution import (
    close_sync_attempt_errors_without_retry__no_commit,
    sync_attempt_error_is_open,
    sync_attempt_error_matches,
)
from onyx.db.enums import (
    IndexAttemptErrorOrder,
    SyncAttemptErrorResolvedReason,
    SyncEntityKind,
)
from onyx.db.models import SyncAttemptError


def _filtered(
    stmt: Select,
    cc_pair_id: int,
    include_resolved: bool,
    attempt_id: int | None,
    entity_kind: SyncEntityKind | None,
    query: str | None,
) -> Select:
    stmt = stmt.where(SyncAttemptError.connector_credential_pair_id == cc_pair_id)
    if not include_resolved:
        stmt = stmt.where(sync_attempt_error_is_open())
    if attempt_id is not None:
        stmt = stmt.where(SyncAttemptError.external_group_sync_attempt_id == attempt_id)
    if entity_kind is not None:
        stmt = stmt.where(SyncAttemptError.entity_kind == entity_kind)
    if query:
        stmt = stmt.where(sync_attempt_error_matches(query))
    return stmt


def _state_rank() -> ColumnElement[int]:
    return case(
        (sync_attempt_error_is_open(), 0),
        (
            SyncAttemptError.resolved_reason
            == SyncAttemptErrorResolvedReason.WITHOUT_RETRY,
            1,
        ),
        else_=2,
    )


def count_sync_attempt_errors(
    db_session: Session,
    cc_pair_id: int,
    include_resolved: bool,
    attempt_id: int | None,
    entity_kind: SyncEntityKind | None,
    query: str | None,
) -> int:
    stmt = _filtered(
        select(func.count(SyncAttemptError.id)),
        cc_pair_id,
        include_resolved,
        attempt_id,
        entity_kind,
        query,
    )
    return db_session.scalar(stmt) or 0


def get_sync_attempt_errors_page(
    db_session: Session,
    cc_pair_id: int,
    include_resolved: bool,
    attempt_id: int | None,
    entity_kind: SyncEntityKind | None,
    query: str | None,
    order: IndexAttemptErrorOrder,
    page_num: int,
    page_size: int,
) -> list[SyncAttemptError]:
    stmt = _filtered(
        select(SyncAttemptError),
        cc_pair_id,
        include_resolved,
        attempt_id,
        entity_kind,
        query,
    )
    if order is IndexAttemptErrorOrder.STATE:
        stmt = stmt.order_by(_state_rank())
    # The id breaks time ties, so a row never moves between pages.
    stmt = (
        stmt.order_by(SyncAttemptError.time_created.desc(), SyncAttemptError.id.desc())
        .offset(page_num * page_size)
        .limit(page_size)
    )
    return list(db_session.scalars(stmt).all())


def resolve_sync_attempt_errors_without_retry__no_commit(
    db_session: Session, cc_pair_id: int, error_ids: Collection[int]
) -> list[int]:
    return close_sync_attempt_errors_without_retry__no_commit(
        db_session,
        SyncAttemptError.id,
        SyncAttemptError.connector_credential_pair_id == cc_pair_id,
        SyncAttemptError.id.in_(error_ids),
    )
