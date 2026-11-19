"""Search mode: the document search experience next to chat.

The flow reuses the chat search building blocks, with no answer generation:

  prepare_search_mode     one DB session: document-set access, ACL, search
                          settings, federated registry, LLM spend checks
  run_search_mode         optional query expansion (rephrase + keywords)
                          -> one search_pipeline lane per query, in parallel
                          -> weighted RRF -> merge_individual_chunks
                          -> save the query to the user's search history
                          -> optional LLM selection of the best documents

It applies the operator-forced document-set scope (FORCED_DOCUMENT_SET_NAMES)
and runs no source or time detection and no Slack lane.
"""

from collections.abc import Callable
from functools import partial

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from onyx.configs.constants import MessageType
from onyx.context.search.models import (
    BaseFilters,
    ChunkSearchRequest,
    InferenceChunk,
    InferenceSection,
    UserAccessFilters,
)
from onyx.context.search.pipeline import merge_individual_chunks, search_pipeline
from onyx.context.search.preprocessing.access_filters import (
    build_access_filters_for_user,
)
from onyx.context.search.utils import convert_inference_sections_to_search_docs
from onyx.db.document_set import require_document_set_access
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.models import User
from onyx.db.search_history import create_search_query
from onyx.db.search_settings import get_current_search_settings
from onyx.document_index.factory import get_default_document_index
from onyx.document_index.interfaces import DocumentIndex
from onyx.federated_connectors.federated_retrieval import (
    FederatedRetrievalInfo,
    get_federated_retrieval_functions,
)
from onyx.llm.factory import get_default_llm, get_search_selection_llm
from onyx.llm.interfaces import LLM
from onyx.natural_language_processing.search_nlp_models import EmbeddingModel
from onyx.secondary_llm_flows.document_filter import select_sections_for_expansion
from onyx.secondary_llm_flows.query_expansion import (
    keyword_query_expansion,
    semantic_query_rephrase,
)
from onyx.server.features.search.search_mode_models import (
    LLMSelectedDocsPacket,
    QueryExpansionsPacket,
    SearchDocsPacket,
    SearchDocWithContent,
    SearchFullResponse,
    SearchStreamPacket,
    SendSearchQueryRequest,
)
from onyx.server.query_and_chat.token_limit import (
    check_model_spend_limits,
    check_token_rate_limits,
)
from onyx.server.usage_limits import check_llm_cost_limit_for_provider
from onyx.tools.models import ChatMinimalTextMessage
from onyx.tools.tool_implementations.search.constants import (
    KEYWORD_QUERY_HYBRID_ALPHA,
    LLM_KEYWORD_QUERY_WEIGHT,
    LLM_SEMANTIC_QUERY_WEIGHT,
    MAX_SELECTED_SECTIONS,
)
from onyx.tools.tool_implementations.search.search_tool import deduplicate_queries
from onyx.tools.tool_implementations.search.search_utils import (
    weighted_reciprocal_rank_fusion,
)
from onyx.utils.logger import setup_logger
from onyx.utils.threadpool_concurrency import run_functions_tuples_in_parallel
from shared_configs.configs import MODEL_SERVER_HOST, MODEL_SERVER_PORT
from shared_configs.contextvars import get_current_tenant_id

logger = setup_logger()

# The user typed these words to find a document, so they rank with the
# rephrased query, not below it as in chat.
ORIGINAL_SEARCH_QUERY_WEIGHT = LLM_SEMANTIC_QUERY_WEIGHT

SearchPacketEmitter = Callable[[SearchStreamPacket], None]


class SearchModePlan(BaseModel):
    """Everything a search needs after the request session closes. The lanes
    run in parallel and must not share an ORM session."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    request: SendSearchQueryRequest
    user: User
    acl_filters: UserAccessFilters
    embedding_model: EmbeddingModel
    document_index: DocumentIndex
    federated_retrieval_infos: list[FederatedRetrievalInfo]
    # None when the request runs no LLM step or no default model exists.
    llm: LLM | None
    selection_llm: LLM | None


def _check_llm_budget(llm: LLM, user: User, db_session: Session) -> None:
    check_llm_cost_limit_for_provider(
        db_session=db_session,
        tenant_id=get_current_tenant_id(),
        llm_provider_api_key=llm.config.api_key,
    )
    check_model_spend_limits(user, llm.config.model_configuration_id)


def resolve_search_mode_llms(
    request: SendSearchQueryRequest, user: User, db_session: Session
) -> tuple[LLM | None, LLM | None]:
    """The query-expansion model and the selection model, after the spend
    checks. Raises the budget error when a limit is reached."""
    if not request.runs_llm_steps:
        return None, None
    try:
        llm = get_default_llm()
    except ValueError:
        logger.warning("Search mode: no default LLM, skipping the LLM steps")
        return None, None

    check_token_rate_limits(user)
    _check_llm_budget(llm, user, db_session)

    selection_llm: LLM | None = None
    if request.runs_llm_selection:
        selection_llm = get_search_selection_llm(fallback=llm)
        if selection_llm is not llm:
            _check_llm_budget(selection_llm, user, db_session)
    return llm, selection_llm


def prepare_search_mode(
    request: SendSearchQueryRequest, user: User, db_session: Session
) -> SearchModePlan:
    filters = request.filters
    # A user may not name a document set they cannot see.
    require_document_set_access(
        db_session, filters.document_set if filters else None, user
    )

    search_settings = get_current_search_settings(db_session)
    embedding_model = EmbeddingModel.from_db_model(
        search_settings=search_settings,
        server_host=MODEL_SERVER_HOST,
        server_port=MODEL_SERVER_PORT,
    )
    federated_retrieval_infos = (
        get_federated_retrieval_functions(
            db_session=db_session,
            user_id=user.id if not user.is_anonymous else None,
            source_types=(
                list(filters.source_type) if filters and filters.source_type else None
            ),
            document_set_names=filters.document_set if filters else None,
        )
        or []
    )
    llm, selection_llm = resolve_search_mode_llms(request, user, db_session)

    return SearchModePlan(
        request=request,
        user=user,
        acl_filters=build_access_filters_for_user(user, db_session),
        embedding_model=embedding_model,
        document_index=get_default_document_index(search_settings, None),
        federated_retrieval_infos=federated_retrieval_infos,
        llm=llm,
        selection_llm=selection_llm,
    )


def _expand_query(query: str, llm: LLM) -> tuple[str | None, list[str]]:
    history = [ChatMinimalTextMessage(message=query, message_type=MessageType.USER)]
    semantic, keywords = run_functions_tuples_in_parallel(
        [
            (semantic_query_rephrase, (history, llm)),
            (keyword_query_expansion, (history, llm)),
        ],
        allow_failures=True,
    )
    return (
        semantic if isinstance(semantic, str) and semantic.strip() else None,
        [kw for kw in keywords or [] if isinstance(kw, str) and kw.strip()],
    )


def build_query_lanes(
    query: str, semantic_query: str | None, keyword_queries: list[str]
) -> list[tuple[str, float, float | None]]:
    """(query, RRF weight, hybrid_alpha) per lane, highest weight first, with
    case-insensitive duplicates merged. A query in both groups keeps one lane,
    the semantic one."""
    semantic_lanes = deduplicate_queries(
        [(query, ORIGINAL_SEARCH_QUERY_WEIGHT)]
        + ([(semantic_query, LLM_SEMANTIC_QUERY_WEIGHT)] if semantic_query else [])
    )
    seen = {lane_query.lower() for lane_query, _ in semantic_lanes}
    keyword_lanes = [
        (kw, weight)
        for kw, weight in deduplicate_queries(
            [(kw, LLM_KEYWORD_QUERY_WEIGHT) for kw in keyword_queries]
        )
        if kw.lower() not in seen
    ]
    lanes: list[tuple[str, float, float | None]] = [
        (lane_query, weight, None) for lane_query, weight in semantic_lanes
    ] + [
        (lane_query, weight, KEYWORD_QUERY_HYBRID_ALPHA)
        for lane_query, weight in keyword_lanes
    ]
    lanes.sort(key=lambda lane: lane[1], reverse=True)
    return lanes


def _run_lane(
    plan: SearchModePlan, query: str, hybrid_alpha: float | None
) -> list[InferenceChunk]:
    return search_pipeline(
        chunk_search_request=ChunkSearchRequest(
            query=query,
            hybrid_alpha=hybrid_alpha,
            user_selected_filters=plan.request.filters,
            limit=plan.request.num_hits,
        ),
        document_index=plan.document_index,
        user=plan.user,
        persona_search_info=None,
        acl_filters=plan.acl_filters,
        embedding_model=plan.embedding_model,
        prefetched_federated_retrieval_infos=plan.federated_retrieval_infos,
        force_configured_document_set_scope=True,
    )


def llm_selected_doc_ids(sections: list[InferenceSection]) -> list[str]:
    return list(dict.fromkeys(s.center_chunk.document_id for s in sections))


def _select_documents(
    plan: SearchModePlan, sections: list[InferenceSection]
) -> list[str] | None:
    """Document ids the LLM picked, best first. None when the step did not run
    or failed: the page still shows the ranked documents."""
    num_docs = plan.request.num_docs_fed_to_llm_selection
    if plan.selection_llm is None or not num_docs or not sections:
        return None
    try:
        selected, _ = select_sections_for_expansion(
            sections=sections[:num_docs],
            user_query=plan.request.search_query,
            llm=plan.selection_llm,
            max_sections=MAX_SELECTED_SECTIONS,
            enforce_llm_input_budget=True,
        )
    except Exception:
        logger.warning("Search mode: LLM document selection failed", exc_info=True)
        return None
    return llm_selected_doc_ids(selected)


def _save_history(user: User, query: str, expansions: list[str]) -> None:
    if user.is_anonymous:
        return
    try:
        with get_session_with_current_tenant() as db_session:
            create_search_query(db_session, user.id, query, expansions)
    except Exception:
        logger.exception("Search mode: failed to save the search history entry")


def _to_search_docs(
    sections: list[InferenceSection], include_content: bool
) -> list[SearchDocWithContent]:
    return [
        SearchDocWithContent(
            **doc.model_dump(),
            content=section.combined_content if include_content else None,
        )
        for doc, section in zip(
            convert_inference_sections_to_search_docs(sections, is_internet=False),
            sections,
            strict=True,
        )
    ]


def run_search_mode(
    plan: SearchModePlan, emit: SearchPacketEmitter | None = None
) -> SearchFullResponse:
    """Run the search. `emit` receives each packet as soon as it is ready."""

    def send(packet: SearchStreamPacket) -> None:
        if emit is not None:
            emit(packet)

    request = plan.request
    query = request.search_query.strip()

    # An explicitly empty source list searches nothing. None means no filter.
    filters: BaseFilters | None = request.filters
    if filters is not None and filters.source_type == []:
        send(SearchDocsPacket(search_docs=[]))
        return SearchFullResponse(all_executed_queries=[query], search_docs=[])

    semantic_query: str | None = None
    keyword_queries: list[str] = []
    if request.run_query_expansion and plan.llm is not None:
        semantic_query, keyword_queries = _expand_query(query, plan.llm)

    lanes = build_query_lanes(query, semantic_query, keyword_queries)
    executed_queries = [lane_query for lane_query, _, _ in lanes]
    if request.run_query_expansion:
        send(QueryExpansionsPacket(executed_queries=executed_queries))

    lane_results: list[list[InferenceChunk]] = run_functions_tuples_in_parallel(
        [
            (partial(_run_lane, plan), (lane_query, hybrid_alpha))
            for lane_query, _, hybrid_alpha in lanes
        ]
    )
    fused_chunks = weighted_reciprocal_rank_fusion(
        ranked_results=lane_results,
        weights=[weight for _, weight, _ in lanes],
        id_extractor=lambda chunk: f"{chunk.document_id}_{chunk.chunk_id}",
    )
    sections = merge_individual_chunks(fused_chunks)[: request.num_hits]
    search_docs = _to_search_docs(sections, request.include_content)
    send(SearchDocsPacket(search_docs=search_docs))

    _save_history(
        plan.user,
        query,
        [lane_query for lane_query in executed_queries if lane_query != query],
    )

    selected_ids = _select_documents(plan, sections)
    if request.runs_llm_selection:
        send(LLMSelectedDocsPacket(llm_selected_doc_ids=selected_ids))

    return SearchFullResponse(
        all_executed_queries=executed_queries,
        search_docs=search_docs,
        llm_selected_doc_ids=selected_ids,
    )
