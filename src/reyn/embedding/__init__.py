"""Reyn embedding infrastructure — public API.

Provider registry pattern:
  - Default provider (``get_provider("litellm")``): RoutingEmbeddingProvider
    which dispatches by resolved model prefix to either the LiteLLM
    backend (= openai/* and other LiteLLM-routable models) or the
    sentence-transformers backend (= ``sentence-transformers/<id>``,
    FP-0043 local-embed extras).
  - The ``"litellm"`` provider name is preserved for backward compat —
    existing consumers receive a wrapper whose LiteLLM-backed behaviour
    is byte-identical to the old direct LiteLLMEmbeddingProvider; the
    only change is that ``sentence-transformers/`` models are now
    routable through the same factory.
  - Operators can register additional providers via register_provider().

Layers (ADR-0033 + FP-0043):
  - LiteLLM passthrough (litellm.aembedding) — ADR-0033 Phase 1
  - Cost preflight estimator
  - Static dimension table
  - Local sentence-transformers backend — FP-0043 Phase 2 (extras-gated)
  - Provider-prefix dispatch routing — FP-0043 Phase 2 Component A
"""
from __future__ import annotations

from typing import Any

from reyn.embedding.cost_estimator import CostEstimate, estimate_indexing_cost
from reyn.embedding.litellm_provider import LiteLLMEmbeddingProvider
from reyn.embedding.provider import EmbedBatchResult, EmbeddingProvider
from reyn.embedding.router_provider import RoutingEmbeddingProvider

# Registry maps provider name → factory class. The ``"litellm"`` slot
# points at the routing wrapper so callers using the historical name
# transparently pick up the local-embed dispatch path; the bare
# ``LiteLLMEmbeddingProvider`` stays available under ``"litellm-only"``
# for tests / callers that want to bypass routing explicitly.
_PROVIDERS: dict[str, type] = {
    "litellm": RoutingEmbeddingProvider,
    "routing": RoutingEmbeddingProvider,
    "litellm-only": LiteLLMEmbeddingProvider,
}


def register_provider(name: str, impl: type[EmbeddingProvider]) -> None:
    """Register a custom EmbeddingProvider implementation.

    Args:
        name: Identifier for the provider (used in get_provider calls).
        impl: Class that implements the EmbeddingProvider protocol.
              Must accept a single ``config`` constructor argument.
    """
    _PROVIDERS[name] = impl


def get_provider(
    name: str = "litellm",
    config: dict | None = None,
    *,
    event_sink: "Any | None" = None,
) -> EmbeddingProvider:
    """Instantiate and return an EmbeddingProvider by name.

    Args:
        name:   Provider name. Default ``"litellm"`` returns the routing
                wrapper (= dispatches by model prefix; sentence-
                transformers backend is reached when the resolved model
                carries the ``sentence-transformers/`` prefix).
        config: Provider configuration dict (e.g. reyn.yaml ``embedding:``
                section). Empty dict used when None.
        event_sink: Optional ``(kind, text, meta) -> None`` callable
                forwarded to backends that emit lifecycle events (= the
                routing wrapper passes it through to the lazily-built
                sentence-transformers backend for model-load
                notifications). Passed only to provider classes that
                accept the kwarg; ignored otherwise.
                FP-0043 Component C.3 onboarding UX.

    Returns:
        An EmbeddingProvider instance.

    Raises:
        KeyError: if name is not registered.
    """
    cls = _PROVIDERS[name]
    if event_sink is not None and cls is RoutingEmbeddingProvider:
        return cls(config or {}, event_sink=event_sink)  # type: ignore[call-arg]
    return cls(config or {})  # type: ignore[call-arg]


__all__ = [
    "EmbeddingProvider",
    "EmbedBatchResult",
    "LiteLLMEmbeddingProvider",
    "RoutingEmbeddingProvider",
    "CostEstimate",
    "estimate_indexing_cost",
    "register_provider",
    "get_provider",
]
