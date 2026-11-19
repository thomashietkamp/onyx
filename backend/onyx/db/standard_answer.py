"""Standard answers: fixed replies the Slack bot posts when a question matches
a keyword rule, grouped into categories that a Slack channel config enables.

Deleting an answer deactivates it, so chat messages that quoted it keep their
link; the keyword's uniqueness counts active answers only. A category still
used by an answer or a channel config cannot be deleted.
"""

import re
from collections.abc import Collection, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from onyx.db.models import (
    SlackChannelConfig__StandardAnswerCategory,
    StandardAnswer,
    StandardAnswer__StandardAnswerCategory,
    StandardAnswerCategory,
)
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.utils.logger import setup_logger

logger = setup_logger()

_WORD_RE = re.compile(r"\w+")


# --- categories ----------------------------------------------------------------


def fetch_standard_answer_categories(
    db_session: Session,
) -> Sequence[StandardAnswerCategory]:
    return db_session.scalars(
        select(StandardAnswerCategory).order_by(StandardAnswerCategory.name)
    ).all()


def fetch_standard_answer_categories_by_ids(
    standard_answer_category_ids: list[int], db_session: Session
) -> Sequence[StandardAnswerCategory]:
    """Dispatched by name from ``onyx/db/slack_channel_config.py``."""
    if not standard_answer_category_ids:
        return []
    return db_session.scalars(
        select(StandardAnswerCategory).where(
            StandardAnswerCategory.id.in_(standard_answer_category_ids)
        )
    ).all()


def _category_or_404(db_session: Session, category_id: int) -> StandardAnswerCategory:
    category = db_session.get(StandardAnswerCategory, category_id)
    if category is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, f"Category {category_id} not found.")
    return category


def _assert_category_name_free(
    db_session: Session, name: str, exclude_id: int | None = None
) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, "A category needs a name.")
    stmt = select(StandardAnswerCategory.id).where(
        StandardAnswerCategory.name == cleaned
    )
    if exclude_id is not None:
        stmt = stmt.where(StandardAnswerCategory.id != exclude_id)
    if db_session.scalar(stmt) is not None:
        raise OnyxError(OnyxErrorCode.CONFLICT, f"Category '{cleaned}' already exists.")
    return cleaned


def insert_standard_answer_category(
    db_session: Session, name: str
) -> StandardAnswerCategory:
    category = StandardAnswerCategory(name=_assert_category_name_free(db_session, name))
    db_session.add(category)
    db_session.commit()
    return category


def update_standard_answer_category(
    db_session: Session, category_id: int, name: str
) -> StandardAnswerCategory:
    category = _category_or_404(db_session, category_id)
    category.name = _assert_category_name_free(db_session, name, exclude_id=category_id)
    db_session.commit()
    return category


def delete_standard_answer_category(db_session: Session, category_id: int) -> None:
    category = _category_or_404(db_session, category_id)
    in_use_by_answer = db_session.scalar(
        select(StandardAnswer__StandardAnswerCategory.standard_answer_id)
        .join(
            StandardAnswer,
            StandardAnswer.id
            == StandardAnswer__StandardAnswerCategory.standard_answer_id,
        )
        .where(
            StandardAnswer__StandardAnswerCategory.standard_answer_category_id
            == category_id,
            StandardAnswer.active.is_(True),
        )
        .limit(1)
    )
    in_use_by_channel = db_session.scalar(
        select(SlackChannelConfig__StandardAnswerCategory.slack_channel_config_id)
        .where(
            SlackChannelConfig__StandardAnswerCategory.standard_answer_category_id
            == category_id
        )
        .limit(1)
    )
    if in_use_by_answer is not None or in_use_by_channel is not None:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            f"Category '{category.name}' is still used by an answer or a Slack channel.",
        )
    db_session.execute(
        delete(StandardAnswer__StandardAnswerCategory).where(
            StandardAnswer__StandardAnswerCategory.standard_answer_category_id
            == category_id
        )
    )
    db_session.delete(category)
    db_session.commit()


# --- answers -------------------------------------------------------------------


def fetch_standard_answers(db_session: Session) -> Sequence[StandardAnswer]:
    return db_session.scalars(
        select(StandardAnswer)
        .where(StandardAnswer.active.is_(True))
        .options(selectinload(StandardAnswer.categories))
        .order_by(StandardAnswer.id)
    ).all()


def _validate_rule(keyword: str, match_regex: bool) -> str:
    cleaned = keyword.strip()
    if not cleaned:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT, "A standard answer needs a keyword."
        )
    if match_regex:
        try:
            re.compile(cleaned)
        except re.error as e:
            raise OnyxError(
                OnyxErrorCode.INVALID_INPUT, f"The keyword is not a valid regex: {e}"
            ) from e
    return cleaned


def _assert_keyword_free(
    db_session: Session, keyword: str, exclude_id: int | None = None
) -> None:
    stmt = select(StandardAnswer.id).where(
        StandardAnswer.keyword == keyword, StandardAnswer.active.is_(True)
    )
    if exclude_id is not None:
        stmt = stmt.where(StandardAnswer.id != exclude_id)
    if db_session.scalar(stmt) is not None:
        raise OnyxError(
            OnyxErrorCode.CONFLICT, f"A standard answer for '{keyword}' already exists."
        )


def _categories(
    db_session: Session, category_ids: Collection[int]
) -> list[StandardAnswerCategory]:
    found = list(
        fetch_standard_answer_categories_by_ids(list(category_ids), db_session)
    )
    if missing := set(category_ids) - {category.id for category in found}:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT, f"Categories not found: {sorted(missing)}"
        )
    return found


def insert_standard_answer(
    db_session: Session,
    *,
    keyword: str,
    answer: str,
    category_ids: Collection[int],
    match_regex: bool,
    match_any_keywords: bool,
) -> StandardAnswer:
    cleaned = _validate_rule(keyword, match_regex)
    _assert_keyword_free(db_session, cleaned)
    standard_answer = StandardAnswer(
        keyword=cleaned,
        answer=answer,
        active=True,
        match_regex=match_regex,
        match_any_keywords=match_any_keywords,
        categories=_categories(db_session, category_ids),
    )
    db_session.add(standard_answer)
    db_session.commit()
    return standard_answer


def _active_answer_or_404(db_session: Session, answer_id: int) -> StandardAnswer:
    standard_answer = db_session.get(StandardAnswer, answer_id)
    if standard_answer is None or not standard_answer.active:
        raise OnyxError(
            OnyxErrorCode.NOT_FOUND, f"Standard answer {answer_id} not found."
        )
    return standard_answer


def update_standard_answer(
    db_session: Session,
    answer_id: int,
    *,
    keyword: str,
    answer: str,
    category_ids: Collection[int],
    match_regex: bool,
    match_any_keywords: bool,
) -> StandardAnswer:
    standard_answer = _active_answer_or_404(db_session, answer_id)
    cleaned = _validate_rule(keyword, match_regex)
    _assert_keyword_free(db_session, cleaned, exclude_id=answer_id)
    standard_answer.keyword = cleaned
    standard_answer.answer = answer
    standard_answer.match_regex = match_regex
    standard_answer.match_any_keywords = match_any_keywords
    standard_answer.categories = _categories(db_session, category_ids)
    db_session.commit()
    return standard_answer


def deactivate_standard_answer(db_session: Session, answer_id: int) -> None:
    _active_answer_or_404(db_session, answer_id).active = False
    db_session.commit()


# --- matching ------------------------------------------------------------------


def answer_matches(standard_answer: StandardAnswer, query: str) -> bool:
    """A regex rule searches the question. A keyword rule compares words: all
    of them must appear, or any one with ``match_any_keywords``."""
    if standard_answer.match_regex:
        try:
            return re.search(standard_answer.keyword, query, re.IGNORECASE) is not None
        except re.error:
            logger.warning("Skipping standard answer %s: bad regex", standard_answer.id)
            return False
    query_words = {word.lower() for word in _WORD_RE.findall(query)}
    keyword_words = {word.lower() for word in _WORD_RE.findall(standard_answer.keyword)}
    if not keyword_words:
        return False
    if standard_answer.match_any_keywords:
        return bool(keyword_words & query_words)
    return keyword_words <= query_words


def find_matching_standard_answers(
    db_session: Session, category_ids: Collection[int], query: str
) -> list[StandardAnswer]:
    if not category_ids or not query.strip():
        return []
    candidates = db_session.scalars(
        select(StandardAnswer)
        .join(
            StandardAnswer__StandardAnswerCategory,
            StandardAnswer__StandardAnswerCategory.standard_answer_id
            == StandardAnswer.id,
        )
        .where(
            StandardAnswer.active.is_(True),
            StandardAnswer__StandardAnswerCategory.standard_answer_category_id.in_(
                category_ids
            ),
        )
        .distinct()
        .order_by(StandardAnswer.id)
    ).all()
    return [candidate for candidate in candidates if answer_matches(candidate, query)]
