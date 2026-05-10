"""Reyn embedding infrastructure — public API (ADR-0033 Phase 1).

Provider registry pattern:
  - Built-in provider: "litellm" → LiteLLMEmbeddingProvider
  - Operators can register custom providers via register_provider()
  - get_provider() is the single factory used by op-handlers

Scope: Phase 1 (1.0 release)
  - LiteLLM passthrough (litellm.aembedding)
  - Cost preflight estimator
  - Static dimension table

Out of scope (Phase 2):
  - Local embedding (sentence-transformers)
  - Dynamic model dimension discovery
  - Config-driven cost table
"""
from __future__ import annotations

from reyn.embedding.cost_estimator import CostEstimate, estimate_indexing_cost
from reyn.embedding.litellm_provider import LiteLLMEmbeddingProvider
from reyn.embedding.provider import EmbedBatchResult, EmbeddingProvider

_PROVIDERS: dict[str, type] = {"litellm": LiteLLMEmbeddingProvider}


def register_provider(name: str, impl: type[EmbeddingProvider]) -> None:
    """Register a custom EmbeddingProvider implementation.

    Args:
        name: Identifier for the provider (used in get_provider calls).
        impl: Class that implements the EmbeddingProvider protocol.
              Must accept a single ``config: dict`` constructor argument.
    """
    _PROVIDERS[name] = impl


def get_provider(
    name: str = "litellm", config: dict | None = None
) -> EmbeddingProvider:
    """Instantiate and return an EmbeddingProvider by name.

    Args:
        name:   Provider name. Default "litellm".
        config: Provider configuration dict (e.g. reyn.yaml ``embedding:``
                section). Empty dict used when None.

    Returns:
        An EmbeddingProvider instance.

    Raises:
        KeyError: if name is not registered.
    """
    cls = _PROVIDERS[name]
    return cls(config or {})  # type: ignore[call-arg]


__all__ = [
    "EmbeddingProvider",
    "EmbedBatchResult",
    "LiteLLMEmbeddingProvider",
    "CostEstimate",
    "estimate_indexing_cost",
    "register_provider",
    "get_provider",
]
