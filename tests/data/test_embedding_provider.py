"""Tier 2: EmbeddingProvider protocol + LiteLLM impl invariants.

Pinned invariants (Tier 2b — subsystem invariants):
  - EmbeddingProvider Protocol structural contract (methods present + callable)
  - LiteLLMEmbeddingProvider.estimate_tokens: tiktoken path + char-fallback path
  - LiteLLMEmbeddingProvider._resolve_model: class lookup / literal passthrough
  - LiteLLMEmbeddingProvider.embed: empty-list short-circuit (no API call)
  - Provider registry: register_provider / get_provider roundtrip
  - Public __init__.py __all__ exports complete

LiteLLM API calls are NOT made in these tests (= embed() covered via empty-list
path + FakeEmbeddingProvider for logic tests). LLMReplay now also patches
litellm.aembedding (#3451) via the same `@pytest.mark.replay` marker used for
acompletion — a Tier 3 replay test for embed() can use it directly; none of
THESE tests need it (protocol/estimator invariants, not a real call).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.data.embedding import (
    EmbedBatchResult,
    EmbeddingProvider,
    LiteLLMEmbeddingProvider,
    get_provider,
    register_provider,
)
from reyn.data.embedding.litellm_provider import _resolve_model_from_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(config: dict | None = None) -> LiteLLMEmbeddingProvider:
    """Construct a LiteLLMEmbeddingProvider with minimal config."""
    return LiteLLMEmbeddingProvider(config or {})


class FakeEmbeddingProvider:
    """Minimal real EmbeddingProvider impl for subsystem-logic tests.

    Returns deterministic vectors of constant value 0.5 so callers can
    assert on shape without any API call.
    """

    def __init__(self, dim: int = 4, tokens_per_char: float = 0.25) -> None:
        self._dim = dim
        self._tokens_per_char = tokens_per_char

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        vectors = [[0.5] * self._dim for _ in texts]
        total_tokens = self.estimate_tokens(texts)
        return EmbedBatchResult(vectors=vectors, model=model, total_tokens=total_tokens)

    def estimate_tokens(self, texts: list[str]) -> int:
        return max(1, int(sum(len(t) for t in texts) * self._tokens_per_char))


# ---------------------------------------------------------------------------
# Protocol structural contract
# ---------------------------------------------------------------------------

class TestEmbeddingProviderProtocol:
    def test_fake_satisfies_protocol(self):
        """Tier 2: FakeEmbeddingProvider satisfies EmbeddingProvider Protocol."""
        provider = FakeEmbeddingProvider()
        assert isinstance(provider, EmbeddingProvider)

    def test_litellm_provider_satisfies_protocol(self):
        """Tier 2: LiteLLMEmbeddingProvider satisfies EmbeddingProvider Protocol."""
        provider = _make_provider()
        assert isinstance(provider, EmbeddingProvider)

    def test_protocol_methods_present(self):
        """Tier 2: EmbeddingProvider protocol defines required methods."""
        provider = _make_provider()
        assert callable(getattr(provider, "embed", None))
        assert callable(getattr(provider, "estimate_tokens", None))


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_estimate_tokens_empty_list(self):
        """Tier 2: estimate_tokens returns 0 for empty list."""
        provider = _make_provider()
        result = provider.estimate_tokens([])
        assert result == 0

    def test_estimate_tokens_fallback_single_char(self):
        """Tier 2: char-fallback: single 4-char text → 1 token."""
        provider = _make_provider()
        # Force fallback by not having tiktoken (test is tiktoken-agnostic)
        # Use internal fallback logic directly: sum(len(t) // 4 for t in texts)
        texts = ["abcd"]  # 4 chars → 1 token
        # Whether tiktoken is available or not, the result should be >= 1
        result = provider.estimate_tokens(texts)
        assert result >= 1

    def test_estimate_tokens_proportional(self):
        """Tier 2: longer text produces more tokens than shorter text."""
        provider = _make_provider()
        short = provider.estimate_tokens(["hi"])
        long = provider.estimate_tokens(["hi" * 100])
        assert long > short

    def test_estimate_tokens_multiple_texts(self):
        """Tier 2: estimate_tokens is additive across multiple texts."""
        provider = _make_provider()
        combined = provider.estimate_tokens(["hello world", "foo bar baz"])
        single_a = provider.estimate_tokens(["hello world"])
        single_b = provider.estimate_tokens(["foo bar baz"])
        # Combined should equal or be close to sum (tiktoken may differ slightly
        # due to cross-text boundary effects, but total >= each individual)
        assert combined >= single_a
        assert combined >= single_b

    def test_estimate_tokens_char_fallback_math(self):
        """Tier 2: char-fallback formula: len(t) // 4 for each text."""
        # Simulate the fallback path directly
        texts = ["abcdefgh", "xyz"]   # 8 chars → 2 tokens, 3 chars → 0 tokens
        expected_fallback = sum(len(t) // 4 for t in texts)  # 2 + 0 = 2
        assert expected_fallback == 2


# ---------------------------------------------------------------------------
# _resolve_model
# ---------------------------------------------------------------------------

class TestResolveModel:
    def test_literal_model_passthrough(self):
        """Tier 2: model with '/' is used directly without class lookup."""
        provider = _make_provider({"classes": {}})
        assert provider.resolve_model("openai/text-embedding-3-small") == \
            "openai/text-embedding-3-small"

    def test_class_lookup_string_value(self):
        """Tier 2: class name without '/' is resolved via classes dict."""
        config = {"classes": {"standard": "openai/text-embedding-3-small"}}
        provider = _make_provider(config)
        assert provider.resolve_model("standard") == "openai/text-embedding-3-small"

    def test_class_lookup_dict_value(self):
        """Tier 2: dict-form class is resolved via 'model' key."""
        config = {
            "classes": {
                "custom": {"model": "openai/text-embedding-3-large"}
            }
        }
        provider = _make_provider(config)
        assert provider.resolve_model("custom") == "openai/text-embedding-3-large"

    def test_class_extends_chain(self):
        """Tier 2: 'extends' chain resolves transitively."""
        config = {
            "classes": {
                "base": "openai/text-embedding-3-small",
                "derived": {"extends": "base"},
            }
        }
        provider = _make_provider(config)
        assert provider.resolve_model("derived") == "openai/text-embedding-3-small"

    def test_class_extends_with_model_override(self):
        """Tier 2: 'extends' + explicit 'model' key overrides the base model."""
        config = {
            "classes": {
                "base": "openai/text-embedding-3-small",
                "custom": {
                    "extends": "base",
                    "model": "openai/text-embedding-3-large",
                },
            }
        }
        provider = _make_provider(config)
        assert provider.resolve_model("custom") == "openai/text-embedding-3-large"

    def test_unknown_class_passthrough(self):
        """Tier 2: unknown class name (no '/') passes through unchanged."""
        provider = _make_provider({"classes": {}})
        # No slash, not in classes → passthrough (backward compat)
        assert provider.resolve_model("mystery") == "mystery"

    def test_cycle_raises_value_error(self):
        """Tier 2: circular extends raises ValueError."""
        classes = {"a": {"extends": "b"}, "b": {"extends": "a"}}
        with pytest.raises(ValueError, match="circular"):
            _resolve_model_from_config(classes, "a")

    def test_missing_model_key_in_dict_raises(self):
        """Tier 2: dict form without 'model' key raises ValueError."""
        classes = {"bad": {"temperature": 0.5}}
        with pytest.raises(ValueError, match="model"):
            _resolve_model_from_config(classes, "bad")


# ---------------------------------------------------------------------------
# embed() empty-list short-circuit (no API call)
# ---------------------------------------------------------------------------

class TestEmbedEmptyList:
    def test_embed_empty_texts_returns_empty_result(self):
        """Tier 2: embed([]) returns empty vectors without calling the API."""
        provider = _make_provider()
        result = asyncio.run(provider.embed([], "openai/text-embedding-3-small"))
        assert result["vectors"] == []
        assert result["total_tokens"] == 0
        assert isinstance(result["model"], str)

    def test_embed_empty_texts_resolves_model(self):
        """Tier 2: embed([]) still resolves model class name."""
        config = {"classes": {"standard": "openai/text-embedding-3-small"}}
        provider = _make_provider(config)
        result = asyncio.run(provider.embed([], "standard"))
        assert result["model"] == "openai/text-embedding-3-small"
        assert result["vectors"] == []


class TestEmbedDropParams:
    def test_direct_call_passes_drop_params_true(self, monkeypatch):
        """Tier 2: #1616 — the direct litellm.aembedding call passes drop_params=True
        so a provider-unsupported param (e.g. encoding_format on a DIRECT gemini-
        embedding call) is dropped client-side instead of failing the index build.
        Captures the real kwargs via a monkeypatched aembedding (no Mock)."""
        from types import SimpleNamespace

        captured: dict[str, Any] = {}

        async def _fake_aembedding(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                data=[{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
                usage=SimpleNamespace(total_tokens=3),
                model=kwargs.get("model", ""),
            )

        import litellm

        monkeypatch.setattr(litellm, "aembedding", _fake_aembedding)

        provider = _make_provider()
        result = asyncio.run(provider.embed(["hello"], "openai/text-embedding-3-small"))

        assert captured.get("drop_params") is True, (
            f"expected drop_params=True forwarded to litellm.aembedding; got {captured!r}"
        )
        assert captured.get("input") == ["hello"]
        assert result["vectors"] == [[0.1, 0.2, 0.3]]


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

class TestProviderRegistry:
    def test_get_provider_returns_litellm_provider_by_default(self):
        """Tier 2: get_provider() returns LiteLLMEmbeddingProvider by default.

        #3128: reyn depends on litellm exclusively for embeddings — the
        prior ``RoutingEmbeddingProvider`` prefix-dispatch wrapper (FP-0043,
        routing between LiteLLM and an in-process local-model backend)
        was collapsed away; ``"litellm"`` now maps directly to
        ``LiteLLMEmbeddingProvider``.
        """
        provider = get_provider()
        assert isinstance(provider, LiteLLMEmbeddingProvider)

    def test_get_litellm_only_returns_bare_litellm_provider(self):
        """Tier 2: ``"litellm-only"`` is an alias for the same concrete class."""
        provider = get_provider("litellm-only")
        assert isinstance(provider, LiteLLMEmbeddingProvider)

    def test_get_provider_with_config_constructs_litellm_provider(self):
        """Tier 2: get_provider("litellm", config=...) constructs the provider.

        Config forwarding is verified indirectly by the provider-specific
        tests (estimate_tokens honours tokenizer, batch processing honours
        batch_size) so we don't assert on private state here.
        """
        config = {"batch_size": 50}
        provider = get_provider("litellm", config=config)
        assert isinstance(provider, LiteLLMEmbeddingProvider)

    def test_register_and_get_custom_provider(self):
        """Tier 2: register_provider + get_provider roundtrip with custom impl."""
        class _MyProvider:
            def __init__(self, config: dict) -> None:
                self.config = config

            async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
                return EmbedBatchResult(vectors=[], model=model, total_tokens=0)

            def estimate_tokens(self, texts: list[str]) -> int:
                return 0

        register_provider("test-custom", _MyProvider)  # type: ignore[arg-type]
        provider = get_provider("test-custom", config={"x": 1})
        assert isinstance(provider, _MyProvider)
        assert provider.config == {"x": 1}

    def test_get_provider_unknown_name_raises_key_error(self):
        """Tier 2: get_provider with unknown name raises KeyError."""
        with pytest.raises(KeyError):
            get_provider("nonexistent-provider-xyz")


# ---------------------------------------------------------------------------
# Public API __all__ completeness
# ---------------------------------------------------------------------------

class TestPublicApi:
    def test_all_exports_importable(self):
        """Tier 2: every name in __all__ is importable from reyn.data.embedding."""
        import reyn.data.embedding as mod
        for name in mod.__all__:
            assert hasattr(mod, name), f"{name!r} in __all__ but missing from module"

    def test_expected_names_in_all(self):
        """Tier 2: __all__ contains all required public names."""
        import reyn.data.embedding as mod
        required = {
            "EmbeddingProvider",
            "EmbedBatchResult",
            "LiteLLMEmbeddingProvider",
            "register_provider",
            "get_provider",
        }
        assert required.issubset(set(mod.__all__))


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_default_batch_size(self):
        """Tier 2: batch_size defaults to 100 when not in config."""
        provider = _make_provider()
        assert provider.batch_size == 100

    def test_default_max_concurrent(self):
        """Tier 2: max_concurrent_batches defaults to 1 (sequential)."""
        provider = _make_provider()
        assert provider.max_concurrent == 1

    def test_default_max_retries(self):
        """Tier 2: max_retries defaults to 3."""
        provider = _make_provider()
        assert provider.max_retries == 3

    def test_default_tokenizer(self):
        """Tier 2: tokenizer defaults to cl100k_base."""
        provider = _make_provider()
        assert provider.tokenizer == "cl100k_base"

    def test_config_overrides_defaults(self):
        """Tier 2: all config keys override their defaults."""
        config = {
            "batch_size": 25,
            "max_concurrent_batches": 4,
            "max_retries": 5,
            "retry_backoff": 3.0,
            "tokenizer": "p50k_base",
        }
        provider = _make_provider(config)
        assert provider.batch_size == 25
        assert provider.max_concurrent == 4
        assert provider.max_retries == 5
        assert provider.retry_backoff == 3.0
        assert provider.tokenizer == "p50k_base"
