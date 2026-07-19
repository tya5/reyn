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

from typing import NotRequired, Protocol, TypedDict, runtime_checkable


class EmbedBatchResult(TypedDict):
    """Result of a single embed() call."""

    vectors: list[list[float]]  # shape: [batch_size, dim]
    model: str                  # canonical model name returned by API
    total_tokens: int           # for cost tracking
    # #3047 (c) — observation-only retry-overhead signal, NOT a cost figure.
    # ``attempts`` = how many times reyn's OWN ``_embed_batch_with_retry`` loop
    # reached ``_aembedding_bounded`` (summed across internal batches); it is
    # reyn's retry-loop altitude, NOT a raw "wire request" count — the two are
    # equal only while #3054's ``max_retries=0`` keeps litellm's SDK-internal
    # retry at 0. ``successful_batches`` = how many internal batches returned a
    # response (each contributing to ``total_tokens``); the operator's
    # retry-overhead is ``attempts - successful_batches``. Both are
    # ``NotRequired`` on purpose: only a provider with a retry loop can populate
    # them, so a loopless test/stub provider omits them rather than fabricating
    # ``attempts=1`` (which would conflate "no retry concept" with "1 attempt" —
    # the #2944 requirement-1 anti-pattern, unready != empty). The ``embed`` op
    # reads them defensively (``result.get("attempts")``) and emits the
    # ``embed_attempts`` audit-event only when present.
    attempts: NotRequired[int]
    successful_batches: NotRequired[int]


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
