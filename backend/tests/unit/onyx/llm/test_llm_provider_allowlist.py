"""LLM_PROVIDER_ALLOWLIST hides and refuses providers it does not name."""

from unittest.mock import patch

from onyx.llm.well_known_providers import llm_provider_options as options


def test_no_allowlist_allows_every_provider() -> None:
    with patch.object(options, "LLM_PROVIDER_ALLOWLIST", frozenset()):
        assert options.llm_provider_allowed("openai")
        assert options.llm_provider_allowed("litellm_proxy")


def test_allowlist_keeps_only_named_providers() -> None:
    with patch.object(options, "LLM_PROVIDER_ALLOWLIST", frozenset({"litellm_proxy"})):
        assert options.llm_provider_allowed("litellm_proxy")
        assert not options.llm_provider_allowed("openai")
        names = {llm.name for llm in options.fetch_available_well_known_llms()}
        assert names <= {"litellm_proxy"}
