"""LiteLLM passthrough implementation of EmbeddingProvider.

Supports:
  - Model class lookup: config["classes"]["standard"] → literal model string
  - Dict-form ModelSpec with `extends` chain resolution (mirrors llm.ModelResolver)
  - Internal batching + concurrency limit via asyncio.Semaphore
  - Exponential-backoff retry on transient errors
  - tiktoken-based token estimation with char-count fallback
  - Static dimension table with 1536 default for unknown models

Design constraints (from task brief):
  - Does NOT import from op_runtime, index, cli, dispatch (P7 / task scope)
  - Does NOT conflate with litellm.acompletion (that's call_llm's concern)
  - Reads proxy config from env (mirrors llm.proxy_kwargs pattern)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from reyn.embedding.provider import EmbedBatchResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dimension table (static; Phase 2 will add dynamic discovery)
# ---------------------------------------------------------------------------

_MODEL_DIMENSIONS: dict[str, int] = {
    "openai/text-embedding-3-small": 1536,
    "openai/text-embedding-3-large": 3072,
    "openai/text-embedding-ada-002": 1536,
    "voyage-3": 1024,
    "voyage-3-lite": 512,
    "cohere/embed-english-v3.0": 1024,
}

_DEFAULT_DIMENSION = 1536  # fallback for unknown models


# ---------------------------------------------------------------------------
# Proxy helper (mirrors llm.proxy_kwargs; kept local to avoid cross-import)
# ---------------------------------------------------------------------------

def _proxy_kwargs() -> dict[str, Any]:
    """Return extra kwargs for litellm.aembedding() when a proxy is configured.

    Reads LITELLM_API_BASE from env (same env var as call_llm uses) so the
    same proxy serves both completions and embeddings.
    """
    api_base = os.environ.get("LITELLM_API_BASE")
    if not api_base:
        return {}
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    return {"api_base": api_base, "custom_llm_provider": "openai", "api_key": api_key}


# ---------------------------------------------------------------------------
# Extends resolution (minimal — mirrors ModelResolver for embedding classes)
# ---------------------------------------------------------------------------

def _resolve_model_from_config(
    classes: dict[str, Any], name: str, seen: frozenset[str] | None = None
) -> str:
    """Resolve a model class name to a LiteLLM model string.

    Handles:
      - str value with "/" → literal (e.g. "openai/text-embedding-3-small")
      - str value without "/" → treat as class reference (recursive lookup)
      - dict with "model" key → extract model string (kwargs ignored for now)
      - dict with "extends" → merge with base recursively

    Returns the resolved LiteLLM model string.
    Raises ValueError on cycle or missing reference.
    """
    if seen is None:
        seen = frozenset()

    if name in seen:
        chain = " -> ".join(list(seen) + [name])
        raise ValueError(f"circular extends in embedding classes: {chain}")

    if name not in classes:
        raise ValueError(
            f"Unknown embedding model class '{name}'. "
            f"Available: {sorted(classes.keys())}"
        )

    seen = seen | {name}
    value = classes[name]

    if isinstance(value, str):
        if "/" in value:
            return value  # literal LiteLLM model string
        # Class reference shorthand — resolve recursively
        return _resolve_model_from_config(classes, value, seen)

    if isinstance(value, dict):
        extends_target = value.get("extends")
        if extends_target is not None:
            # Resolve base, then let dict's "model" override if present
            base_model = _resolve_model_from_config(classes, str(extends_target), seen)
            return value.get("model", base_model)
        if "model" not in value:
            raise ValueError(
                f"Embedding class '{name}' dict form requires a 'model' key; "
                f"got keys: {list(value.keys())}"
            )
        return value["model"]

    raise ValueError(
        f"Embedding class '{name}' must be str or dict, got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# LiteLLMEmbeddingProvider
# ---------------------------------------------------------------------------

class LiteLLMEmbeddingProvider:
    """EmbeddingProvider backed by litellm.aembedding().

    Args:
        config: reyn.yaml's ``embedding:`` section as a plain dict.
            Required keys (with defaults):
              classes           dict of model class name → model spec
              batch_size        int (default 100)
              max_concurrent_batches  int (default 1 = sequential)
              max_retries       int (default 3)
              retry_backoff     float (default 2.0, base for 2^attempt seconds)
            Optional:
              tokenizer         str (default "cl100k_base")
    """

    def __init__(self, config: "dict[str, Any] | Any | None" = None) -> None:
        # Accept either:
        #   (a) a plain dict (legacy / test path), or
        #   (b) an EmbeddingConfig dataclass (production path from load_config()).
        # Distinguished by presence of the dataclass-only ``classes`` attribute
        # carrying EmbeddingClassSpec values; we duck-type to avoid an
        # import cycle on reyn.config from this leaf module.
        if config is None:
            config = {}
        if not isinstance(config, dict) and hasattr(config, "classes"):
            # EmbeddingConfig dataclass: classes is dict[str, EmbeddingClassSpec].
            # Flatten to the str-form expected by _resolve_model_from_config.
            self._classes = {
                name: spec.model for name, spec in config.classes.items()
            }
            self._batch_size = int(config.batch_size)
            self._max_concurrent = int(config.max_concurrent_batches)
            self._max_retries = int(config.max_retries)
            # Literal[exponential|linear] → numeric base used in 2^attempt sleep.
            self._retry_backoff = (
                2.0 if config.retry_backoff == "exponential" else 1.5
            )
            self.tokenizer = str(config.tokenizer)
        else:
            self._classes: dict[str, Any] = config.get("classes", {})
            self._batch_size: int = int(config.get("batch_size", 100))
            self._max_concurrent: int = int(
                config.get("max_concurrent_batches", 1)
            )
            self._max_retries: int = int(config.get("max_retries", 3))
            self._retry_backoff: float = float(config.get("retry_backoff", 2.0))
            self.tokenizer: str = config.get("tokenizer", "cl100k_base")

    # ── Config read-only accessors ────────────────────────────────────────
    # Tier-C1 cleanup: expose the init-time-frozen config fields via
    # ``@property`` so callers (= tests, future tooling) can read them
    # without reaching for ``_batch_size`` etc. Pure read-only — the
    # provider does not mutate these after ``__init__``.

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def retry_backoff(self) -> float:
        return self._retry_backoff

    # ── Model resolution ───────────────────────────────────────────────────

    def resolve_model(self, model: str) -> str:
        """Resolve a model class name or literal string to a LiteLLM model.

        If model contains "/" it's already a literal string — use directly.
        Otherwise look it up in the configured classes map.
        If not found in classes, treat as a passthrough (backward compatible).
        """
        if "/" in model:
            return model
        if model in self._classes:
            return _resolve_model_from_config(self._classes, model)
        # Unknown class without "/": pass through unchanged (legacy compat)
        return model

    # ── Token estimation ───────────────────────────────────────────────────

    def estimate_tokens(self, texts: list[str]) -> int:
        """Rough token count for cost preflight. Never calls the API.

        Uses tiktoken when available; falls back to ~4 chars/token heuristic.
        """
        try:
            import tiktoken
            enc = tiktoken.get_encoding(self.tokenizer)
            return sum(len(enc.encode(t)) for t in texts)
        except Exception:
            # Fallback: rough estimate ~4 chars/token
            return sum(len(t) // 4 for t in texts)

    # ── Dimension lookup ───────────────────────────────────────────────────

    def get_dimension(self, model: str) -> int:
        """Return vector dimension for a model. Uses static table; defaults 1536."""
        resolved = self.resolve_model(model)
        return _MODEL_DIMENSIONS.get(resolved, _DEFAULT_DIMENSION)

    # ── Core embed ─────────────────────────────────────────────────────────

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        """Embed a batch of texts via litellm.aembedding().

        Internally splits into batches of batch_size, runs batches with
        max_concurrent_batches concurrency, and aggregates results in order.

        Args:
            texts: List of strings to embed.
            model: Model class name or literal LiteLLM model string.

        Returns:
            EmbedBatchResult with vectors, canonical model name, total_tokens.
        """
        if not texts:
            resolved = self.resolve_model(model)
            return EmbedBatchResult(vectors=[], model=resolved, total_tokens=0)

        resolved_model = self.resolve_model(model)

        # Split into batches
        batches: list[list[str]] = []
        for i in range(0, len(texts), self._batch_size):
            batches.append(texts[i : i + self._batch_size])

        sem = asyncio.Semaphore(self._max_concurrent)

        async def _embed_batch(batch: list[str]) -> EmbedBatchResult:
            async with sem:
                return await self._embed_batch_with_retry(batch, resolved_model)

        results = await asyncio.gather(*(_embed_batch(b) for b in batches))

        # Aggregate in order
        all_vectors: list[list[float]] = []
        total_tokens = 0
        canonical_model = resolved_model
        for r in results:
            all_vectors.extend(r["vectors"])
            total_tokens += r["total_tokens"]
            if r["model"]:
                canonical_model = r["model"]

        return EmbedBatchResult(
            vectors=all_vectors,
            model=canonical_model,
            total_tokens=total_tokens,
        )

    async def _embed_batch_with_retry(
        self, batch: list[str], resolved_model: str
    ) -> EmbedBatchResult:
        """Call litellm.aembedding() with exponential-backoff retry."""
        extra = _proxy_kwargs()
        # Strip provider prefix when routing via local proxy (mirrors call_llm)
        effective_model = (
            resolved_model.split("/", 1)[1] if extra and "/" in resolved_model
            else resolved_model
        )

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            if attempt > 0:
                wait = self._retry_backoff ** attempt
                await asyncio.sleep(wait)
            try:
                import litellm
                response = await litellm.aembedding(
                    model=effective_model,
                    input=batch,
                    **extra,
                )
                return self._parse_response(response, resolved_model)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "embed batch attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_retries,
                    exc,
                )

        raise RuntimeError(
            f"Embedding failed after {self._max_retries} attempts. "
            f"Last error: {last_exc}"
        ) from last_exc

    @staticmethod
    def _parse_response(response: Any, fallback_model: str) -> EmbedBatchResult:
        """Extract vectors and token usage from a litellm embedding response."""
        vectors: list[list[float]] = []
        total_tokens = 0
        canonical_model = fallback_model

        # litellm EmbeddingResponse.data is a list whose items can be either:
        #   (a) Embedding objects with .index / .embedding attrs (typical), or
        #   (b) plain dicts {"index": int, "embedding": [...]} (some
        #       provider passthroughs / proxy paths return this shape).
        # Tolerate both so we don't lose vectors when the provider chooses
        # the dict serialisation.
        def _idx(e: Any) -> int:
            return e["index"] if isinstance(e, dict) else getattr(e, "index", 0)

        def _vec(e: Any) -> list[float]:
            v = e["embedding"] if isinstance(e, dict) else getattr(e, "embedding", [])
            return list(v)

        try:
            items = sorted(response.data, key=_idx)
            vectors = [_vec(e) for e in items]
        except Exception as exc:
            logger.warning("failed to parse embedding vectors: %s", exc)

        # Token usage
        try:
            usage = response.usage
            if usage is not None:
                total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        except Exception:
            pass

        # Canonical model from response
        try:
            if response.model:
                canonical_model = response.model
        except Exception:
            pass

        return EmbedBatchResult(
            vectors=vectors,
            model=canonical_model,
            total_tokens=total_tokens,
        )
