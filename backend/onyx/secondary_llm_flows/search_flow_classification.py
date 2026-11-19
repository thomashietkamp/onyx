"""Decide if a query from the new-session input goes to search mode or to chat."""

from onyx.llm.interfaces import LLM, GenerationContext
from onyx.llm.models import (
    GenerationOptions,
    GenerationRequest,
    ReasoningEffort,
    SystemMessage,
    UserMessage,
)
from onyx.prompts.search_flow_classification import (
    SEARCH_FLOW_CLASSIFICATION_SYSTEM_PROMPT,
    SEARCH_FLOW_CLASSIFICATION_USER_PROMPT,
)
from onyx.tracing.flows import LLMFlow
from onyx.utils.logger import setup_logger

logger = setup_logger()

# Longer input is an instruction or a question, not a document lookup. It goes
# to chat with no LLM call.
MAX_SEARCH_FLOW_WORDS = 30
# Without an LLM verdict, only short input with no question mark is a search.
HEURISTIC_MAX_SEARCH_WORDS = 6
# Room for one word, plus models that add punctuation or a short preamble.
_CLASSIFICATION_MAX_TOKENS = 20


def heuristic_is_search_flow(user_query: str) -> bool:
    query = user_query.strip()
    if not query or "?" in query or "\n" in query:
        return False
    return len(query.split()) <= HEURISTIC_MAX_SEARCH_WORDS


def parse_search_flow_verdict(text: str) -> bool | None:
    """True for SEARCH, False for CHAT, None when the reply names neither."""
    normalized = text.strip().upper()
    has_search = "SEARCH" in normalized
    has_chat = "CHAT" in normalized
    if has_search == has_chat:
        return None
    return has_search


def classify_search_flow(user_query: str, llm: LLM | None) -> bool:
    """Return True when the query is a document search.

    With no LLM, or when the LLM call fails or gives no clear verdict, the
    word-count heuristic decides.
    """
    query = user_query.strip()
    if not query or len(query.split()) > MAX_SEARCH_FLOW_WORDS:
        return False
    if llm is None:
        return heuristic_is_search_flow(query)

    try:
        response = llm.invoke(
            GenerationRequest(
                messages=[
                    SystemMessage(content=SEARCH_FLOW_CLASSIFICATION_SYSTEM_PROMPT),
                    UserMessage(
                        content=SEARCH_FLOW_CLASSIFICATION_USER_PROMPT.format(
                            user_query=query
                        )
                    ),
                ],
                options=GenerationOptions(
                    reasoning_effort=ReasoningEffort.OFF,
                    max_tokens=_CLASSIFICATION_MAX_TOKENS,
                ),
            ),
            context=GenerationContext(flow=LLMFlow.SEARCH_FLOW_CLASSIFICATION),
        )
    except Exception:
        logger.warning(
            "Search flow classification failed, using the heuristic", exc_info=True
        )
        return heuristic_is_search_flow(query)

    verdict = parse_search_flow_verdict(response.text or "")
    if verdict is None:
        return heuristic_is_search_flow(query)
    return verdict
