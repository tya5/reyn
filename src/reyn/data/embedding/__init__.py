"""Reyn embedding infrastructure — public API.

Provider registry pattern:
  - Default provider (``get_provider("litellm")``): ``LiteLLMEmbeddingProvider``,
    the sole embedding backend. Reyn depends on litellm exclusively for
    embeddings — no in-process model backend — so every embedding class
    (``light`` / ``standard`` / ``strong`` and any operator-defined class in
    ``embedding.classes``) resolves through litellm's provider routing
    (``openai/*`` and any other litellm-routable model string).
  - Operators can register additional providers via register_provider().

History: FP-0043 Phase 2 added a local in-process embedding-model backend behind
a ``RoutingEmbeddingProvider`` prefix-dispatch wrapper. #3128 removed the
in-process backend (reyn depends on litellm exclusively; local embedding
models are reached, if desired, via a litellm-fronted proxy) — the wrapper
had become a pure pass-through to ``LiteLLMEmbeddingProvider`` and was
collapsed away. ``get_provider("litellm")`` now returns
``LiteLLMEmbeddingProvider`` directly.

Layers (ADR-0033):
  - LiteLLM passthrough (litellm.aembedding) — ADR-0033 Phase 1
  - Cost preflight estimator
  - Static dimension table
"""
from __future__ import annotations

import inspect
from typing import Any

from reyn.data.embedding.cost_estimator import CostEstimate, estimate_indexing_cost
from reyn.data.embedding.litellm_provider import LiteLLMEmbeddingProvider
from reyn.data.embedding.provider import EmbedBatchResult, EmbeddingProvider

# Registry maps provider name → factory class. The ``"litellm"`` slot is the
# default and only built-in backend; ``"litellm-only"`` is kept as an alias
# for callers/tests that want to name the concrete class explicitly.
_PROVIDERS: dict[str, type] = {
    "litellm": LiteLLMEmbeddingProvider,
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
        name:   Provider name. Default ``"litellm"`` returns
                ``LiteLLMEmbeddingProvider``.
        config: Provider configuration dict (e.g. reyn.yaml ``embedding:``
                section). Empty dict used when None.
        event_sink: Optional ``(kind, text, meta) -> None`` callable forwarded
                to backends that accept the kwarg. ``LiteLLMEmbeddingProvider``
                does not accept it (no lazy-load lifecycle to report on), so
                it is silently ignored for the default provider — kept on the
                signature for registered custom providers that do consume it.

    Returns:
        An EmbeddingProvider instance.

    Raises:
        KeyError: if name is not registered.
    """
    cls = _PROVIDERS[name]
    if event_sink is not None and "event_sink" in inspect.signature(cls).parameters:
        return cls(config or {}, event_sink=event_sink)  # type: ignore[call-arg]
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
