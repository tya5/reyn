"""Cost preflight estimator for embedding indexing jobs (UX gap fix B).

Called by the index_docs Phase 1 preprocessor before starting Phase 2
to give operators a cost estimate and optional confirmation gate.

Design:
  - Extrapolates from a sample of chunks rather than counting all chunks
    (= cheap: avoid iterating 100K-item list just for preflight)
  - USD/1M-token rates are hard-coded for Phase 1; Phase 2 will make them
    config-driven via reyn.yaml ``embedding.cost_per_m_tokens``
  - Never calls the embedding API — pure local computation
"""
from __future__ import annotations

from dataclasses import dataclass

from reyn.embedding.provider import EmbeddingProvider

# ---------------------------------------------------------------------------
# Cost table (USD per 1M tokens, as of 2026)
# Phase 2: move to reyn.yaml embedding.cost_per_m_tokens
# ---------------------------------------------------------------------------

_MODEL_COST_PER_M_TOKENS: dict[str, float] = {
    "openai/text-embedding-3-small": 0.02,
    "openai/text-embedding-3-large": 0.13,
    "openai/text-embedding-ada-002": 0.10,
    "voyage-3": 0.18,
    "voyage-3-lite": 0.02,
    "cohere/embed-english-v3.0": 0.10,
}

_DEFAULT_COST_PER_M = 0.02  # fallback: cheapest known tier


# ---------------------------------------------------------------------------
# CostEstimate dataclass
# ---------------------------------------------------------------------------

@dataclass
class CostEstimate:
    """Preflight cost estimate for a bulk embedding job."""

    chunk_count: int
    estimated_tokens: int
    estimated_cost_usd: float
    model: str


# ---------------------------------------------------------------------------
# Estimation function
# ---------------------------------------------------------------------------

def estimate_indexing_cost(
    provider: EmbeddingProvider,
    samples: list[str],
    total_chunk_count: int,
    model: str,
) -> CostEstimate:
    """Extrapolate embedding cost from a sample.

    Estimates total tokens as:
        avg_tokens_per_sample * total_chunk_count

    Args:
        provider:          An EmbeddingProvider instance (used for token estimation).
        samples:           A representative subset of chunks (e.g. first 10).
                           Empty list → 0 cost estimate.
        total_chunk_count: Total number of chunks to be embedded.
        model:             Model class name or literal LiteLLM model string.
                           Must match a key in the cost table or will use default.

    Returns:
        CostEstimate with chunk_count, estimated_tokens, estimated_cost_usd, model.
    """
    if not samples:
        return CostEstimate(
            chunk_count=total_chunk_count,
            estimated_tokens=0,
            estimated_cost_usd=0.0,
            model=model,
        )

    sample_tokens = provider.estimate_tokens(samples)
    avg_per_chunk = sample_tokens / len(samples)
    total_tokens = int(avg_per_chunk * total_chunk_count)

    rate = _MODEL_COST_PER_M_TOKENS.get(model, _DEFAULT_COST_PER_M)
    cost = total_tokens / 1_000_000 * rate

    return CostEstimate(
        chunk_count=total_chunk_count,
        estimated_tokens=total_tokens,
        estimated_cost_usd=cost,
        model=model,
    )
