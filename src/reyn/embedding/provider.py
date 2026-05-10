"""EmbeddingProvider protocol + EmbedBatchResult TypedDict.

All embedding providers must implement this protocol so the OS can swap
backends without knowing provider details (P7 — no provider-specific strings
in OS code).

EmbedBatchResult carries the minimal data the OS and op-handlers need:
  - vectors: shape [batch_size, dim] (order matches input texts)
  - model: canonical model name as returned by the provider API
  - total_tokens: consumed tokens for cost tracking / budget integration
"""
from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


class EmbedBatchResult(TypedDict):
    """Result of a single embed() call."""

    vectors: list[list[float]]  # shape: [batch_size, dim]
    model: str                  # canonical model name returned by API
    total_tokens: int           # for cost tracking


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers.

    Implementations must be async-safe and handle internal batching so that
    callers can pass arbitrarily large text lists without worrying about API
    request size limits.
    """

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        """Embed a batch of texts. May internally split into multiple API calls.

        Args:
            texts: List of strings to embed. Empty list returns empty vectors.
            model: Model class name (e.g. "standard") or literal LiteLLM
                   model string (e.g. "openai/text-embedding-3-small").

        Returns:
            EmbedBatchResult with vectors in the same order as input texts.
        """
        ...

    def estimate_tokens(self, texts: list[str]) -> int:
        """Rough token count for cost preflight (UX gap B).

        Used by estimate_indexing_cost before starting a large indexing job.
        Does NOT call the API — pure local estimation.

        Args:
            texts: List of strings to count tokens for.

        Returns:
            Estimated total token count across all texts.
        """
        ...

    def get_dimension(self, model: str) -> int:
        """Return vector dimension for a model.

        Used by IndexBackend to pre-allocate storage and validate vector
        compatibility when mixing models is disallowed.

        Args:
            model: Model class name or literal LiteLLM model string.

        Returns:
            Vector dimension (e.g. 1536 for text-embedding-3-small).
            Falls back to 1536 for unknown models.
        """
        ...
