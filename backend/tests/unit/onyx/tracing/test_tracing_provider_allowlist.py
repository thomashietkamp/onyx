"""TRACING_PROVIDER_ALLOWLIST drops providers it does not name, from env vars
and stored rows alike, so they never receive traces."""

from unittest.mock import patch

from onyx.tracing import provider_config
from onyx.tracing.provider_config import (
    BraintrustConfig,
    EffectiveTracingConfig,
    LangfuseConfig,
)
from shared_configs.enums import TracingProviderType

_BOTH = EffectiveTracingConfig(
    braintrust=BraintrustConfig(api_key="bt", project="p"),
    langfuse=LangfuseConfig(secret_key="sk", public_key="pk", host=None),
)


def test_no_allowlist_keeps_every_provider() -> None:
    with (
        patch.object(provider_config, "TRACING_PROVIDER_ALLOWLIST", frozenset()),
        patch.object(
            provider_config, "_resolve_effective_tracing_config", return_value=_BOTH
        ),
    ):
        config = provider_config.resolve_effective_tracing_config()
    assert config.braintrust is not None and config.langfuse is not None


def test_langfuse_only_drops_braintrust() -> None:
    with (
        patch.object(
            provider_config, "TRACING_PROVIDER_ALLOWLIST", frozenset({"langfuse"})
        ),
        patch.object(
            provider_config, "_resolve_effective_tracing_config", return_value=_BOTH
        ),
    ):
        config = provider_config.resolve_effective_tracing_config()
        assert not provider_config.tracing_provider_allowed(
            TracingProviderType.BRAINTRUST
        )
    assert config.braintrust is None
    assert config.langfuse is not None
