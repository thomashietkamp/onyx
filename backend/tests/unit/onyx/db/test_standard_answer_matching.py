import pytest

from onyx.db.models import StandardAnswer
from onyx.db.standard_answer import answer_matches


def _answer(
    keyword: str, *, regex: bool = False, any_word: bool = False
) -> StandardAnswer:
    return StandardAnswer(
        keyword=keyword,
        answer="x",
        active=True,
        match_regex=regex,
        match_any_keywords=any_word,
    )


@pytest.mark.parametrize(
    ("keyword", "query", "expected"),
    [
        ("it ticket", "How do I open a ticket with IT?", True),
        ("it ticket", "How do I open a ticket?", False),
        ("vpn", "vpnclient broken", False),
    ],
)
def test_all_keywords(keyword: str, query: str, expected: bool) -> None:
    assert answer_matches(_answer(keyword), query) is expected


def test_any_keyword() -> None:
    rule = _answer("ticket problem issue", any_word=True)
    assert answer_matches(rule, "I have a problem")
    assert not answer_matches(rule, "all good here")


def test_regex_is_case_insensitive_search() -> None:
    rule = _answer(r"(?:it|support)\s*ticket", regex=True)
    assert answer_matches(rule, "Need a Support Ticket please")
    assert not answer_matches(rule, "need help")


def test_broken_regex_never_matches() -> None:
    assert not answer_matches(_answer("(unclosed", regex=True), "(unclosed")
