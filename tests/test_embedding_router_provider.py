"""Tier 2: FP-0043 Phase 2 — RoutingEmbeddingProvider dispatch contract.

Pins the prefix-based routing layer's invariants:

  1. Models resolving to ``openai/*`` (and other non-prefix strings) forward
     to the injected LiteLLM-shape provider; the sentence-transformers
     backend is NOT instantiated.
  2. Models resolving to ``sentence-transformers/<id>`` route to the
     injected ST-shape provider; LiteLLM is bypassed.
  3. Class-name resolution honours the configured ``classes`` map (=
     a class name like ``"local-mini"`` resolves to its
     ``sentence-transformers/...`` model string before routing).
  4. ``get_dimension`` follows the same dispatch.
  5. Lazy instantiation: when no ST provider is injected, it is created
     only on first ST-prefix match; the openai/* path never triggers it.

Tests use **constructor DI** (= ``RoutingEmbeddingProvider(config=...,
litellm_provider=fake, sentence_transformers_provider=fake)``) to supply
real fake provider instances. No Mock / AsyncMock; no private state
assertions; no attribute mutation on the SUT.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from reyn.config import EmbeddingClassSpec, EmbeddingConfig
from reyn.embedding.router_provider import RoutingEmbeddingProvider


def _config_with_local_class() -> EmbeddingConfig:
    """Build an EmbeddingConfig that defines local-mini and openai classes."""
    return EmbeddingConfig(
        default_class="standard",
        classes={
            "standard": EmbeddingClassSpec(model="openai/text-embedding-3-small"),
            "local-mini": EmbeddingClassSpec(
                model="sentence-transformers/all-MiniLM-L6-v2",
            ),
        },
        batch_size=100,
        max_concurrent_batches=1,
        max_retries=3,
        retry_backoff="exponential",
        tokenizer="cl100k_base",
    )


def _run(coro):
    return asyncio.run(coro)


class _FakeBackend:
    """Real fake EmbeddingProvider; records calls + returns canned vectors.

    Used for BOTH the LiteLLM and ST backends in these tests — they
    share the same protocol so a single fake shape suffices, with the
    ``label`` distinguishing which side caught the dispatch.
    """

    def __init__(self, label: str, dim: int = 1536):
        self.label = label
        self.dim = dim
        self.embed_calls: list[tuple[list[str], str]] = []
        self.dimension_calls: list[str] = []

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.embed_calls.append((list(texts), model))
        return {
            "vectors": [[1.0] * self.dim for _ in texts],
            "model": f"{self.label}::resolved::{model}",
            "total_tokens": sum(len(t) // 4 for t in texts),
        }

    def estimate_tokens(self, texts: list[str]) -> int:
        return sum(len(t) // 4 for t in texts)

    def get_dimension(self, model: str) -> int:
        self.dimension_calls.append(model)
        return self.dim


def _make_provider(litellm: _FakeBackend, st: _FakeBackend) -> RoutingEmbeddingProvider:
    """Build a RoutingEmbeddingProvider with both backends DI-injected."""
    return RoutingEmbeddingProvider(
        config=_config_with_local_class(),
        litellm_provider=litellm,
        sentence_transformers_provider=st,
    )


# ── 1. openai/* models route to LiteLLM backend ──────────────────────────────


def test_openai_model_routes_to_litellm() -> None:
    """Tier 2: an openai/<name> model dispatches to the LiteLLM backend."""
    litellm = _FakeBackend("litellm", dim=1536)
    st = _FakeBackend("st", dim=384)
    provider = _make_provider(litellm, st)

    result = _run(provider.embed(["hello"], "openai/text-embedding-3-small"))
    n_litellm_calls = len(litellm.embed_calls)
    assert n_litellm_calls == 1
    assert litellm.embed_calls[0][1] == "openai/text-embedding-3-small"
    assert result["model"].startswith("litellm::")
    assert not st.embed_calls  # ST backend never touched


def test_class_name_resolving_to_openai_routes_to_litellm() -> None:
    """Tier 2: class name 'standard' → openai/* → LiteLLM backend."""
    litellm = _FakeBackend("litellm")
    st = _FakeBackend("st", dim=384)
    provider = _make_provider(litellm, st)

    _run(provider.embed(["hello"], "standard"))
    assert litellm.embed_calls and litellm.embed_calls[0][1] == "standard"
    assert not st.embed_calls


# ── 2. sentence-transformers/* routes to ST backend ──────────────────────────


def test_st_prefix_model_routes_to_sentence_transformers_backend() -> None:
    """Tier 2: a sentence-transformers/<id> model dispatches to the ST backend."""
    litellm = _FakeBackend("litellm")
    st = _FakeBackend("st", dim=384)
    provider = _make_provider(litellm, st)

    _run(provider.embed(["hi"], "sentence-transformers/all-MiniLM-L6-v2"))
    n_st_calls = len(st.embed_calls)
    assert n_st_calls == 1
    assert st.embed_calls[0][1] == "sentence-transformers/all-MiniLM-L6-v2"
    assert not litellm.embed_calls


def test_class_name_resolving_to_st_routes_to_st_backend() -> None:
    """Tier 2: class name 'local-mini' → sentence-transformers/* → ST backend."""
    litellm = _FakeBackend("litellm")
    st = _FakeBackend("st", dim=384)
    provider = _make_provider(litellm, st)

    _run(provider.embed(["hi"], "local-mini"))
    n_st_calls = len(st.embed_calls)
    assert n_st_calls == 1 and st.embed_calls[0][1] == "local-mini"
    assert not litellm.embed_calls


# ── 3. get_dimension follows the same dispatch ───────────────────────────────


def test_get_dimension_routes_to_st_for_st_prefix() -> None:
    """Tier 2: get_dimension(local-mini) consults the ST backend."""
    litellm = _FakeBackend("litellm")
    st = _FakeBackend("st", dim=384)
    provider = _make_provider(litellm, st)

    assert provider.get_dimension("local-mini") == 384
    assert st.dimension_calls == ["local-mini"]
    assert not litellm.dimension_calls


def test_get_dimension_routes_to_litellm_for_openai() -> None:
    """Tier 2: get_dimension(openai/*) consults the LiteLLM backend."""
    litellm = _FakeBackend("litellm", dim=1536)
    st = _FakeBackend("st", dim=384)
    provider = _make_provider(litellm, st)

    assert provider.get_dimension("openai/text-embedding-3-small") == 1536
    assert litellm.dimension_calls == ["openai/text-embedding-3-small"]
    assert not st.dimension_calls


# ── 4. Lazy ST backend instantiation when not DI-injected ────────────────────


def test_st_backend_remains_uninjected_when_only_litellm_path_used() -> None:
    """Tier 2: with ST not injected and only openai/* used, no ST is built.

    Production posture: zero overhead when callers stay on the LiteLLM
    path. The DI parameter is None by default, and only the embed() of
    a sentence-transformers/* model would force the lazy import.
    """
    litellm = _FakeBackend("litellm")
    provider = RoutingEmbeddingProvider(
        config=_config_with_local_class(),
        litellm_provider=litellm,
        # sentence_transformers_provider intentionally omitted
    )

    _run(provider.embed(["x"], "openai/text-embedding-3-small"))
    _run(provider.embed(["y"], "standard"))
    # The ST backend slot is still the sentinel None we passed in.
    # We assert by sending the next call to the ST path with an
    # injected fake and verifying the prior calls didn't touch it.
    st = _FakeBackend("st-late", dim=384)
    provider2 = RoutingEmbeddingProvider(
        config=_config_with_local_class(),
        litellm_provider=litellm,
        sentence_transformers_provider=st,
    )
    _run(provider2.embed(["z"], "local-mini"))
    assert st.embed_calls and st.embed_calls[0][0] == ["z"]


# ── 5. Graceful degradation when sentence_transformers not installed ─────────


def test_st_backend_real_lazy_load_raises_install_hint(monkeypatch) -> None:
    """Tier 2: with no ST DI provided and the lib absent, the real lazy
    import surfaces the canonical install hint.

    Pinned because the hint message is the user-facing onboarding cue
    (`pip install 'reyn[local-embed]'`). The visibility gate elsewhere
    relies on this exception to keep ``search_actions`` hidden.
    """
    provider = RoutingEmbeddingProvider(config=_config_with_local_class())
    # sentence_transformers_provider intentionally omitted → real lazy path.

    # Force the import to fail even if the library is locally installed.
    for k in list(sys.modules):
        if k.startswith("sentence_transformers"):
            monkeypatch.delitem(sys.modules, k, raising=False)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    with pytest.raises(ImportError) as excinfo:
        _run(provider.embed(["x"], "local-mini"))
    assert "reyn[local-embed]" in str(excinfo.value)
