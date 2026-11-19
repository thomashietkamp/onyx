"""Search mode: flow classification, the search run with a mocked index, and
the search history the run saves."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from onyx.configs.constants import DocumentSource
from onyx.context.search.models import (
    BaseFilters,
    ChunkSearchRequest,
    InferenceChunk,
    InferenceSection,
    UserAccessFilters,
)
from onyx.db.models import User
from onyx.db.search_history import create_search_query, fetch_search_queries_for_user
from onyx.document_index.interfaces import DocumentIndex
from onyx.llm.interfaces import LLM
from onyx.natural_language_processing.search_nlp_models import EmbeddingModel
from onyx.secondary_llm_flows.search_flow_classification import (
    MAX_SEARCH_FLOW_WORDS,
    classify_search_flow,
    parse_search_flow_verdict,
)
from onyx.server.features.search import search_mode
from onyx.server.features.search.search_mode import (
    SearchModePlan,
    build_query_lanes,
    run_search_mode,
)
from onyx.server.features.search.search_mode_models import (
    LLMSelectedDocsPacket,
    QueryExpansionsPacket,
    SearchDocsPacket,
    SearchStreamPacket,
    SendSearchQueryRequest,
)
from onyx.tracing.flows import LLMFlow
from tests.external_dependency_unit.conftest import MakeTestUser
from tests.external_dependency_unit.mock_search_pipeline import (
    MockInternalSearchResult,
    run_functions_tuples_sequential,
)

# ---------------------------------------------------------------------------
# Flow classification
# ---------------------------------------------------------------------------


def _llm_replying(text: str) -> MagicMock:
    llm = MagicMock(spec=LLM)
    llm.invoke.return_value = MagicMock(text=text)
    return llm


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("SEARCH", True), ("chat", False), ("Answer: SEARCH.", True)],
)
def test_classification_follows_llm_verdict(reply: str, expected: bool) -> None:
    llm = _llm_replying(reply)
    assert classify_search_flow("what is our refund policy?", llm) is expected
    context = llm.invoke.call_args.kwargs["context"]
    assert context.flow == LLMFlow.SEARCH_FLOW_CLASSIFICATION


def test_classification_falls_back_to_heuristic() -> None:
    # No clear verdict: short input is a search, a question is chat.
    assert classify_search_flow("q3 roadmap", _llm_replying("maybe")) is True
    assert classify_search_flow("where is it?", _llm_replying("")) is False

    failing = MagicMock(spec=LLM)
    failing.invoke.side_effect = RuntimeError("provider down")
    assert classify_search_flow("onboarding guide", failing) is True

    assert classify_search_flow("pricing page", None) is True
    assert parse_search_flow_verdict("SEARCH or CHAT") is None


def test_long_or_empty_input_is_chat_without_llm_call() -> None:
    llm = _llm_replying("SEARCH")
    long_query = " ".join(["word"] * (MAX_SEARCH_FLOW_WORDS + 1))
    assert classify_search_flow(long_query, llm) is False
    assert classify_search_flow("   ", llm) is False
    llm.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Search run
# ---------------------------------------------------------------------------


def _chunk(doc_id: str, chunk_ind: int = 0) -> InferenceChunk:
    return MockInternalSearchResult(
        document_id=doc_id,
        source_type=DocumentSource.CONFLUENCE,
        semantic_identifier=f"Doc {doc_id}",
        chunk_ind=chunk_ind,
    ).to_inference_chunk()


def _plan(
    user: User, request: SendSearchQueryRequest, llm: LLM | None
) -> SearchModePlan:
    return SearchModePlan(
        request=request,
        user=user,
        acl_filters=UserAccessFilters(access_control_list=[], cc_pair_access=None),
        embedding_model=MagicMock(spec=EmbeddingModel),
        document_index=MagicMock(spec=DocumentIndex),
        federated_retrieval_infos=[],
        llm=llm,
        selection_llm=llm,
    )


def _run(
    plan: SearchModePlan,
    results_by_query: dict[str, list[InferenceChunk]],
    select: Callable[..., tuple[list[InferenceSection], list[str] | None]],
) -> tuple[Any, list[SearchStreamPacket], list[ChunkSearchRequest]]:
    packets: list[SearchStreamPacket] = []
    requests: list[ChunkSearchRequest] = []

    def fake_pipeline(
        chunk_search_request: ChunkSearchRequest, **kwargs: Any
    ) -> list[InferenceChunk]:
        assert kwargs["force_configured_document_set_scope"] is True
        assert kwargs["acl_filters"] is plan.acl_filters
        requests.append(chunk_search_request)
        return results_by_query.get(chunk_search_request.query, [])

    with (
        patch.object(search_mode, "search_pipeline", side_effect=fake_pipeline),
        patch.object(
            search_mode, "select_sections_for_expansion", side_effect=select
        ) as selection,
        patch.object(
            search_mode, "semantic_query_rephrase", return_value="rephrased query"
        ),
        patch.object(
            search_mode, "keyword_query_expansion", return_value=["kw one", "kw two"]
        ),
        patch.object(
            search_mode,
            "run_functions_tuples_in_parallel",
            side_effect=run_functions_tuples_sequential,
        ),
    ):
        response = run_search_mode(plan, packets.append)
    if not plan.request.runs_llm_selection:
        selection.assert_not_called()
    return response, packets, requests


def test_search_runs_expansion_selection_and_saves_history(
    db_session: Session, tenant_context: None, make_test_user: MakeTestUser
) -> None:
    del tenant_context
    user = make_test_user("search_mode")
    request = SendSearchQueryRequest(
        search_query="vacation policy",
        run_query_expansion=True,
        num_docs_fed_to_llm_selection=10,
        include_content=True,
        stream=True,
    )

    def select(
        sections: list[InferenceSection], **_: Any
    ) -> tuple[list[InferenceSection], None]:
        # The LLM picks the last document only.
        return [sections[-1]], None

    response, packets, requests = _run(
        _plan(user, request, MagicMock(spec=LLM)),
        {
            "vacation policy": [_chunk("a"), _chunk("b")],
            "rephrased query": [_chunk("b"), _chunk("c")],
            "kw one": [_chunk("c")],
        },
        select,
    )

    assert [type(packet) for packet in packets] == [
        QueryExpansionsPacket,
        SearchDocsPacket,
        LLMSelectedDocsPacket,
    ]
    assert {r.query for r in requests} == {
        "vacation policy",
        "rephrased query",
        "kw one",
        "kw two",
    }
    doc_ids = [doc.document_id for doc in response.search_docs]
    assert sorted(doc_ids) == ["CONFLUENCE_a", "CONFLUENCE_b", "CONFLUENCE_c"]
    # Found by two lanes, so it ranks first.
    assert doc_ids[0] in ("CONFLUENCE_b", "CONFLUENCE_c")
    assert all(doc.content is not None for doc in response.search_docs)
    assert response.llm_selected_doc_ids == [doc_ids[-1]]

    history = fetch_search_queries_for_user(db_session, user.id, limit=10)
    assert [entry.query for entry in history] == ["vacation policy"]
    assert set(history[0].query_expansions or []) == {
        "rephrased query",
        "kw one",
        "kw two",
    }


def test_search_without_llm_steps_runs_one_lane(
    db_session: Session, tenant_context: None, make_test_user: MakeTestUser
) -> None:
    del tenant_context
    user = make_test_user("search_mode_plain")
    request = SendSearchQueryRequest(search_query="handbook", num_hits=1)

    def never_select(**_: Any) -> tuple[list[InferenceSection], None]:
        raise AssertionError("selection must not run")

    response, packets, requests = _run(
        _plan(user, request, None),
        {"handbook": [_chunk("x"), _chunk("y")]},
        never_select,
    )

    assert [r.query for r in requests] == ["handbook"]
    assert [type(packet) for packet in packets] == [SearchDocsPacket]
    assert [doc.document_id for doc in response.search_docs] == ["CONFLUENCE_x"]
    assert response.search_docs[0].content is None
    assert response.llm_selected_doc_ids is None
    history = fetch_search_queries_for_user(db_session, user.id, limit=10)
    assert [(e.query, e.query_expansions) for e in history] == [("handbook", None)]


def test_empty_source_list_searches_nothing(
    tenant_context: None, make_test_user: MakeTestUser
) -> None:
    del tenant_context
    user = make_test_user("search_mode_empty")
    request = SendSearchQueryRequest(
        search_query="anything", filters=BaseFilters(source_type=[])
    )

    response, packets, requests = _run(
        _plan(user, request, None), {"anything": [_chunk("z")]}, MagicMock()
    )

    assert requests == []
    assert response.search_docs == []
    assert packets == [SearchDocsPacket(search_docs=[])]


def test_query_lanes_merge_duplicates() -> None:
    lanes = build_query_lanes("Pricing", "pricing", ["pricing", "price list"])
    assert [(query, alpha) for query, _, alpha in lanes] == [
        ("Pricing", None),
        ("price list", 0.2),
    ]


# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------


def test_search_history_is_per_user_newest_first(
    db_session: Session, tenant_context: None, make_test_user: MakeTestUser
) -> None:
    del tenant_context
    user = make_test_user("search_history")
    other = make_test_user("search_history_other")

    old = create_search_query(db_session, user.id, "old query", None)
    old.created_at = datetime.now(UTC) - timedelta(days=10)
    db_session.commit()
    create_search_query(db_session, user.id, "first", ["first expanded"])
    newest = create_search_query(db_session, user.id, "second", None)
    newest.created_at = datetime.now(UTC) + timedelta(seconds=5)
    db_session.commit()
    create_search_query(db_session, other.id, "not mine", None)

    entries = fetch_search_queries_for_user(db_session, user.id, limit=10)
    assert [entry.query for entry in entries] == ["second", "first", "old query"]
    assert entries[1].query_expansions == ["first expanded"]

    limited = fetch_search_queries_for_user(db_session, user.id, limit=1)
    assert [entry.query for entry in limited] == ["second"]

    recent = fetch_search_queries_for_user(db_session, user.id, limit=10, filter_days=7)
    assert [entry.query for entry in recent] == ["second", "first"]
