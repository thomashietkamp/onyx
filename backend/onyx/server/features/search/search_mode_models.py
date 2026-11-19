"""Request and response models of the search mode routes (/search/send-search-message,
/search/search-flow-classification, /search/search-history)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from onyx.context.search.models import BaseFilters, SearchDoc
from onyx.db.models import SearchQuery

DEFAULT_SEARCH_MODE_NUM_HITS = 30
MAX_SEARCH_MODE_NUM_HITS = 100
# Sections the selection LLM reads at most. More would not fit its prompt.
MAX_DOCS_FED_TO_LLM_SELECTION = 30
DEFAULT_SEARCH_HISTORY_LIMIT = 100
MAX_SEARCH_HISTORY_LIMIT = 1000


class SearchFlowClassificationRequest(BaseModel):
    user_query: str = Field(..., max_length=8192)


class SearchFlowClassificationResponse(BaseModel):
    is_search_flow: bool


class SendSearchQueryRequest(BaseModel):
    search_query: str = Field(..., min_length=1, max_length=2048)
    filters: BaseFilters | None = None
    # Top sections the LLM reads to pick the best documents. None or 0 skips
    # the LLM selection step.
    num_docs_fed_to_llm_selection: int | None = Field(
        default=None, ge=0, le=MAX_DOCS_FED_TO_LLM_SELECTION
    )
    run_query_expansion: bool = False
    num_hits: int = Field(
        default=DEFAULT_SEARCH_MODE_NUM_HITS, ge=1, le=MAX_SEARCH_MODE_NUM_HITS
    )
    include_content: bool = False
    stream: bool = False

    @property
    def runs_llm_selection(self) -> bool:
        return bool(self.num_docs_fed_to_llm_selection)

    @property
    def runs_llm_steps(self) -> bool:
        return self.run_query_expansion or self.runs_llm_selection


class SearchDocWithContent(SearchDoc):
    content: str | None = None


class SearchFullResponse(BaseModel):
    all_executed_queries: list[str]
    search_docs: list[SearchDocWithContent]
    doc_selection_reasoning: str | None = None
    llm_selected_doc_ids: list[str] | None = None
    error: str | None = None


class SearchDocsPacket(BaseModel):
    type: Literal["search_docs"] = "search_docs"
    search_docs: list[SearchDocWithContent]


class SearchErrorPacket(BaseModel):
    type: Literal["search_error"] = "search_error"
    error: str


class LLMSelectedDocsPacket(BaseModel):
    type: Literal["llm_selected_docs"] = "llm_selected_docs"
    llm_selected_doc_ids: list[str] | None


class QueryExpansionsPacket(BaseModel):
    type: Literal["query_expansions"] = "query_expansions"
    executed_queries: list[str]


SearchStreamPacket = (
    SearchDocsPacket | SearchErrorPacket | LLMSelectedDocsPacket | QueryExpansionsPacket
)


class SearchQueryResponse(BaseModel):
    query: str
    query_expansions: list[str] | None
    created_at: datetime

    @classmethod
    def from_model(cls, search_query: SearchQuery) -> "SearchQueryResponse":
        return cls(
            query=search_query.query,
            query_expansions=search_query.query_expansions,
            created_at=search_query.created_at,
        )


class SearchHistoryResponse(BaseModel):
    search_queries: list[SearchQueryResponse]
