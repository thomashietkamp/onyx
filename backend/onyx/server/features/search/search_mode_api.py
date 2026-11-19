"""Search mode routes: the document search experience next to chat.

POST /search/send-search-message      ranked documents, optional LLM picks
POST /search/search-flow-classification  routes new-session input to search or chat
GET  /search/search-history           the user's past search mode queries

The public search API (POST /search, POST /search/raw) lives in api.py.
"""

import queue
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.models import User
from onyx.db.search_history import fetch_search_queries_for_user
from onyx.error_handling.exceptions import OnyxError
from onyx.llm.factory import get_default_llm
from onyx.llm.interfaces import LLM
from onyx.secondary_llm_flows.search_flow_classification import (
    classify_search_flow,
)
from onyx.server.features.search.search_mode import (
    SearchModePlan,
    prepare_search_mode,
    run_search_mode,
)
from onyx.server.features.search.search_mode_models import (
    DEFAULT_SEARCH_HISTORY_LIMIT,
    MAX_SEARCH_HISTORY_LIMIT,
    SearchErrorPacket,
    SearchFlowClassificationRequest,
    SearchFlowClassificationResponse,
    SearchFullResponse,
    SearchHistoryResponse,
    SearchQueryResponse,
    SearchStreamPacket,
    SendSearchQueryRequest,
)
from onyx.server.query_and_chat.token_limit import (
    check_model_spend_limits,
    check_token_rate_limits,
)
from onyx.server.settings.store import load_settings
from onyx.server.usage_limits import check_llm_cost_limit_for_provider
from onyx.server.utils import get_json_line
from onyx.server.utils_vector_db import require_vector_db
from onyx.tracing.framework.create import SearchTraceMetadata, trace
from onyx.utils.fleet_query_telemetry import QueryMode, telemetry_query
from onyx.utils.logger import setup_logger
from onyx.utils.threadpool_concurrency import start_thread_with_context
from shared_configs.contextvars import get_current_tenant_id

logger = setup_logger()

router = APIRouter(prefix="/search")

_GENERIC_SEARCH_ERROR = "Search failed. Try again."


class _StreamEnd:
    """Marks the end of the packet queue."""


_STREAM_END = _StreamEnd()


def _trace_metadata(user: User) -> dict[str, object]:
    return SearchTraceMetadata(user_id=str(user.id)).model_dump()


def _traced_search(
    plan: SearchModePlan,
    emit: "queue.Queue[SearchStreamPacket | _StreamEnd] | None" = None,
) -> SearchFullResponse:
    with trace("search_mode", metadata=_trace_metadata(plan.user)):
        return run_search_mode(plan, emit.put if emit is not None else None)


@telemetry_query(mode=QueryMode.SEARCH)
def _full_search(plan: SearchModePlan) -> SearchFullResponse:
    return _traced_search(plan)


@telemetry_query(mode=QueryMode.SEARCH)
def _stream_search(plan: SearchModePlan) -> Iterator[SearchStreamPacket]:
    """Run the search in a worker thread, so the trace context stays open
    across packets, and yield each packet as it arrives."""
    packets: queue.Queue[SearchStreamPacket | _StreamEnd] = queue.Queue()

    def work() -> None:
        try:
            _traced_search(plan, packets)
        except OnyxError as error:
            packets.put(SearchErrorPacket(error=error.detail))
        except Exception:
            logger.exception("Search mode: search failed")
            packets.put(SearchErrorPacket(error=_GENERIC_SEARCH_ERROR))
        finally:
            packets.put(_STREAM_END)

    start_thread_with_context(work, name="search-mode-stream", daemon=True)
    while True:
        packet = packets.get()
        if isinstance(packet, _StreamEnd):
            return
        yield packet


@router.post(
    "/send-search-message",
    dependencies=[Depends(require_vector_db)],
    # The route returns JSON or a packet stream, so no single response model fits.
    response_model=None,
)
def send_search_message(
    request: SendSearchQueryRequest,
    user: User = Depends(require_permission(Permission.READ_SEARCH)),
    db_session: Session = Depends(get_session),
) -> SearchFullResponse | StreamingResponse:
    """Search the index for documents, without an answer. With `stream`, the
    response is newline-delimited JSON packets."""
    # Budget and access errors return before any packet is sent.
    plan = prepare_search_mode(request, user, db_session)

    if not request.stream:
        return _full_search(plan)

    def lines() -> Iterator[str]:
        for packet in _stream_search(plan):
            yield get_json_line(packet.model_dump(mode="json"))

    return StreamingResponse(lines(), media_type="application/x-ndjson")


def _classification_llm(user: User, db_session: Session) -> LLM | None:
    """The default model, or None when there is none or a budget is spent.
    Classification only routes the query, so it never blocks the user."""
    try:
        llm = get_default_llm()
    except ValueError:
        return None
    try:
        check_token_rate_limits(user)
        check_llm_cost_limit_for_provider(
            db_session=db_session,
            tenant_id=get_current_tenant_id(),
            llm_provider_api_key=llm.config.api_key,
        )
        check_model_spend_limits(user, llm.config.model_configuration_id)
    except OnyxError:
        return None
    return llm


@router.post("/search-flow-classification", dependencies=[Depends(require_vector_db)])
def search_flow_classification(
    request: SearchFlowClassificationRequest,
    user: User = Depends(require_permission(Permission.READ_SEARCH)),
    db_session: Session = Depends(get_session),
) -> SearchFlowClassificationResponse:
    """Decide if a new-session query goes to search mode or to chat."""
    if load_settings().search_ui_enabled is False:
        return SearchFlowClassificationResponse(is_search_flow=False)

    llm = _classification_llm(user, db_session)
    with trace("search_flow_classification", metadata=_trace_metadata(user)):
        is_search_flow = classify_search_flow(request.user_query, llm)
    return SearchFlowClassificationResponse(is_search_flow=is_search_flow)


@router.get("/search-history")
def get_search_history(
    limit: int = Query(
        default=DEFAULT_SEARCH_HISTORY_LIMIT, ge=1, le=MAX_SEARCH_HISTORY_LIMIT
    ),
    filter_days: int | None = Query(default=None, ge=1),
    user: User = Depends(require_permission(Permission.READ_SEARCH)),
    db_session: Session = Depends(get_session),
) -> SearchHistoryResponse:
    """The user's search mode queries, newest first."""
    if user.is_anonymous:
        return SearchHistoryResponse(search_queries=[])
    return SearchHistoryResponse(
        search_queries=[
            SearchQueryResponse.from_model(search_query)
            for search_query in fetch_search_queries_for_user(
                db_session, user.id, limit=limit, filter_days=filter_days
            )
        ]
    )
