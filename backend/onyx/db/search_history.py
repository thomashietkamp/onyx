"""Search mode history: one `search_query` row per search a user runs."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.db.models import SearchQuery


def create_search_query(
    db_session: Session,
    user_id: UUID,
    query: str,
    query_expansions: list[str] | None,
) -> SearchQuery:
    search_query = SearchQuery(
        user_id=user_id,
        query=query,
        query_expansions=query_expansions or None,
    )
    db_session.add(search_query)
    db_session.commit()
    return search_query


def fetch_search_queries_for_user(
    db_session: Session,
    user_id: UUID,
    limit: int,
    filter_days: int | None = None,
) -> Sequence[SearchQuery]:
    """Newest first. `filter_days` keeps only the queries of the last N days."""
    stmt = select(SearchQuery).where(SearchQuery.user_id == user_id)
    if filter_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=filter_days)
        stmt = stmt.where(SearchQuery.created_at >= cutoff)
    stmt = stmt.order_by(SearchQuery.created_at.desc(), SearchQuery.id).limit(limit)
    return db_session.scalars(stmt).all()
