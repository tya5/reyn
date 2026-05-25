"""RoutingEmbeddingProvider — dispatch by resolved model prefix.

FP-0043 Phase 2 Component A. Sits at the top of the embedding provider
chain and routes each ``.embed()`` / ``.get_dimension()`` call to one
of two backends based on the **resolved model string**:

    sentence-transformers/<id>  → SentenceTransformersEmbeddingProvider
    everything else             → LiteLLMEmbeddingProvider
                                  (= existing openai/* + other LiteLLM-
                                   routable providers, unchanged)

This is the single integration point for adding new local backends
later (= GGUF / ONNX / etc., per FP-0043 Non-goals defer). New
backends slot into ``_BACKENDS_BY_PREFIX`` without consumer changes.

Lazy instantiation
------------------
The sentence-transformers backend is **only** instantiated on first
prefix-match (= same lazy posture as the underlying provider's model
load). Consumers paying $0 for the LiteLLM path see zero overhead
from this layer.

Behaviour preservation
----------------------
For any model whose resolved string lacks the ``sentence-transformers/``
prefix, calls forward verbatim to the wrapped LiteLLMEmbeddingProvider.
There is no path where this wrapper changes the LiteLLM backend's
behaviour; the existing openai/* + other LiteLLM-routable providers
are byte-identical to pre-FP-0043.
"""
from __future__ import annotations

from typing import Any

from reyn.embedding.litellm_provider import LiteLLMEmbeddingProvider
from reyn.embedding.provider import EmbedBatchResult


def _resolve_via_classes(
    classes: dict, model: str,
) -> str:
    """Resolve a class name through the configured classes map.

    Mirrors LiteLLMEmbeddingProvider._resolve_model but in a free
    function so the routing layer can inspect the resolved string
    without instantiating the litellm provider first.
    """
    if "/" in model:
        return model
    if model in classes:
        spec = classes[model]
        return spec.model if hasattr(spec, "model") else str(spec)
    return model


class RoutingEmbeddingProvider:
    """EmbeddingProvider that dispatches by resolved model prefix.

    Args:
        config: ``EmbeddingConfig`` dataclass or plain dict. Forwarded
            verbatim to the underlying backends; this wrapper owns no
            state of its own besides the lazily-constructed backend
            instances.
        litellm_provider: Optional pre-built EmbeddingProvider to use
            for non-prefixed (= LiteLLM-routable) models. When None,
            a fresh ``LiteLLMEmbeddingProvider(config=config)`` is
            constructed. Exposed for **structural dependency injection
            in tests** so they can supply a real fake provider via a
            keyword argument instead of mutating private attributes.
        sentence_transformers_provider: Same shape for the
            sentence-transformers backend. When None, the backend is
            instantiated lazily on first prefix-match (= production
            posture: zero overhead when only LiteLLM-routable models
            are used). Tests pass a fake here to verify dispatch.
    """

    def __init__(
        self,
        config: "dict[str, Any] | Any | None" = None,
        *,
        litellm_provider: "Any | None" = None,
        sentence_transformers_provider: "Any | None" = None,
    ) -> None:
        self._config = config
        self._litellm = (
            litellm_provider
            if litellm_provider is not None
            else LiteLLMEmbeddingProvider(config=config)
        )
        self._st: Any = sentence_transformers_provider  # lazy when None

        # Cache the classes map for prefix resolution without re-walking
        # the EmbeddingConfig structure on every embed() call.
        if config is None:
            classes: dict = {}
        elif hasattr(config, "classes"):
            classes = dict(config.classes)
        elif isinstance(config, dict):
            classes = config.get("classes", {}) or {}
        else:
            classes = {}
        self._classes = classes
        # Inherit tokenizer attribute from the litellm backend so callers
        # treating tokenizer as a public attribute keep working.
        self.tokenizer = getattr(self._litellm, "tokenizer", "cl100k_base")

    # ── Internal dispatch ──────────────────────────────────────────────────

    def _backend_for(self, model: str) -> Any:
        """Pick the right backend for the resolved-model string."""
        from reyn.embedding.sentence_transformers_provider import _PREFIX
        resolved = _resolve_via_classes(self._classes, model)
        if resolved.startswith(_PREFIX):
            if self._st is None:
                from reyn.embedding.sentence_transformers_provider import (
                    SentenceTransformersEmbeddingProvider,
                )
                self._st = SentenceTransformersEmbeddingProvider(
                    config=self._config,
                )
            return self._st
        return self._litellm

    # ── EmbeddingProvider protocol ─────────────────────────────────────────

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        return await self._backend_for(model).embed(texts, model)

    def estimate_tokens(self, texts: list[str]) -> int:
        # Token estimation is provider-agnostic in practice (both backends
        # use the same char/4 heuristic on tiktoken failure). We delegate
        # to litellm by default; this keeps tokenizer-aware estimation
        # available even when the user is in "local" mode.
        return self._litellm.estimate_tokens(texts)

    def get_dimension(self, model: str) -> int:
        return self._backend_for(model).get_dimension(model)


__all__ = ["RoutingEmbeddingProvider"]
