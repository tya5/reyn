"""LiteLLM passthrough implementation of EmbeddingProvider.

Supports:
  - Model class lookup: config["classes"]["standard"] → literal model string
  - Dict-form ModelSpec with `extends` chain resolution (mirrors llm.ModelResolver)
  - Internal batching + concurrency limit via asyncio.Semaphore
  - A finite per-attempt bound on every aembedding call (#3043) —
    `embedding.timeout`, default 60.0s, `<= 0` opts out. Applied HERE (not at
    the op) so it covers every caller of this provider by construction. The
    matching cancel half is the `embed` op's `race_cancellable` seam, which is
    at the op because that is where `ctx.cancel_event` lives and because every
    embedding egress in the OS funnels through that op.
    The bound is a LATENCY invariant, not a COST one — it caps how long we
    wait, not how many requests the provider receives. The COST invariant
    (#3047) is `max_retries=0` passed explicitly to every `litellm.aembedding`
    call in `_aembedding_bounded`, so the OpenAI SDK client cannot silently
    retry underneath reyn's own retry loop (unset -> litellm's
    `DEFAULT_MAX_RETRIES` = 2 SDK-internal retries per attempt, 9 requests
    across `max_retries: 3` instead of 3). See `_aembedding_bounded` for the
    measured detail.
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

from reyn.data.embedding.provider import EmbedBatchResult

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
# Per-attempt bound (#3043)
# ---------------------------------------------------------------------------

#: Per-attempt deadline for ONE ``litellm.aembedding`` call, in seconds.
#: Matches ``chat.timeout.llm_call_seconds`` (``reyn.config.chat``): an embedding
#: call is the same KIND of external call as a chat LLM call — one HTTP round-trip
#: to a model provider — so it carries the same bound. (The MCP gateway's 120.0 is
#: deliberately NOT the mirror target for the VALUE: an MCP call also pays a
#: subprocess spawn, which an embedding call does not. It IS the mirror target for
#: the SHAPE — see ``_bounded_timeout`` and the op-level cancel race.)
#: Without a bound, litellm's own ``request_timeout`` default (6000.0 = 100 min per
#: attempt, ~5h across ``max_retries``) is the only ceiling — a hang, to an operator.
_DEFAULT_EMBED_TIMEOUT_SECONDS: float = 60.0


def resolve_embed_timeout(config: "dict[str, Any] | Any") -> "float | None":
    """Per-attempt embedding timeout: the finite default, overridden by ``timeout``
    (``embedding.timeout`` in reyn.yaml); ``<= 0`` opts out (no bound). A malformed
    value falls back to the default — fail-safe, i.e. keep a finite bound.

    Mirrors :func:`reyn.mcp.gateway.resolve_call_timeout` (the same contract, so an
    operator who knows one knob knows the other), reading from either the
    ``EmbeddingConfig`` dataclass or the legacy plain-dict config form.
    """
    raw = (
        config.get("timeout")
        if isinstance(config, dict)
        else getattr(config, "timeout", None)
    )
    timeout: "float | None" = _DEFAULT_EMBED_TIMEOUT_SECONDS
    if raw is not None:
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            timeout = _DEFAULT_EMBED_TIMEOUT_SECONDS
    if timeout is not None and timeout <= 0:
        return None
    return timeout


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
              timeout           float (default 60.0; <= 0 opts out) — the
                                per-attempt deadline for one aembedding call
                                (#3043), resolved via resolve_embed_timeout.
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
        # #3043: resolved from EITHER config form by the same function, so the
        # dict (legacy / test) path and the dataclass (production) path cannot
        # disagree about the bound. None = operator opted out (`timeout <= 0`).
        self._timeout: "float | None" = resolve_embed_timeout(config)

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

    @property
    def timeout(self) -> "float | None":
        """Per-attempt bound in seconds; ``None`` = operator opted out (#3043)."""
        return self._timeout

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
        # #3047 (c): fold the per-batch retry-loop counts up the SAME way
        # ``total_tokens`` is folded — no new aggregation machinery. Each
        # per-batch result carries these because it went through the retry loop;
        # ``.get(..., 0)`` stays defensive in case a future batch path omits them.
        total_attempts = 0
        successful_batches = 0
        canonical_model = resolved_model
        for r in results:
            all_vectors.extend(r["vectors"])
            total_tokens += r["total_tokens"]
            total_attempts += r.get("attempts", 0)
            successful_batches += r.get("successful_batches", 0)
            if r["model"]:
                canonical_model = r["model"]

        return EmbedBatchResult(
            vectors=all_vectors,
            model=canonical_model,
            total_tokens=total_tokens,
            attempts=total_attempts,
            successful_batches=successful_batches,
        )

    async def _embed_batch_with_retry(
        self, batch: list[str], resolved_model: str
    ) -> EmbedBatchResult:
        """Call litellm.aembedding() with a per-attempt bound + exponential-backoff retry.

        #3043: the bound is applied TWICE, deliberately — mirroring
        ``MCPGateway._run``, which likewise passes ``timeout_seconds`` INTO the
        client and ALSO wraps the await in ``asyncio.timeout``:

          - ``timeout=`` into ``aembedding`` lets litellm enforce its own HTTP
            deadline and tear its connection down cleanly (and raise its own
            ``Timeout``, which the retry loop below then treats as any other
            transient error);
          - ``asyncio.timeout`` around the await is the STRUCTURAL ceiling that
            holds even if litellm ignores, mishandles, or reinterprets the kwarg.
            The bound must not depend on the provider library's cooperation —
            that dependency is exactly what made 6000s the real ceiling here.

        The two are NOT the same deadline, and that is precisely why both are here
        (measured, #3047): litellm forwards ``timeout=`` to the OpenAI SDK client as
        a PER-HTTP-REQUEST deadline, and that client retries INTERNALLY
        (``max_retries=2`` by default), so ``timeout=2`` alone was measured taking
        7.9s — 3 requests, not one. ``asyncio.timeout`` is the altitude an operator
        actually reads this knob at: one deadline for the whole attempt. Without the
        wrap, ``timeout: 60`` would silently mean up to 180s per attempt.

        ★ SCOPE — this bounds WAITING, not SPENDING. Left alone, the OpenAI SDK
        client retries INTERNALLY under this bound (litellm's implicit
        ``max_retries or DEFAULT_MAX_RETRIES`` -> 2), so one attempt could
        deliver up to 3 requests and ``max_retries: 3`` up to 9 — measured, all
        9 delivered in 7.6s against a fast-erroring provider with the default
        60.0s bound never engaging. ``_aembedding_bounded`` closes that lever
        with an explicit ``max_retries=0``, collapsing 9 -> 3: reyn's own retry
        loop here is then the ONLY retry layer (#3047).
        """
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
                # perf/log-routing chokepoint: an embedding/RAG-first path can
                # be the FIRST litellm import in the process — funnel it through
                # ensure_litellm_ready() so litellm's import-time console log
                # routing (#2929) wraps it too (idempotent; cheap on 2nd+ call).
                from reyn.llm.litellm_bootstrap import ensure_litellm_ready
                ensure_litellm_ready()
                import litellm
                response = await self._aembedding_bounded(
                    litellm,
                    model=effective_model,
                    input=batch,
                    # #1616: drop provider-unsupported params (e.g. encoding_format
                    # on a DIRECT gemini-embedding call) — the litellm-recommended
                    # client default for embeddings. Fixes the direct / non-proxy
                    # provider-mismatch; a harmless no-op when routing via a proxy
                    # (there the param is added/rejected proxy-side, so the proxy
                    # needs `litellm_settings: drop_params: true` — see the
                    # action-index-build-failed operator guidance + docs).
                    drop_params=True,
                    **extra,
                )
                result = self._parse_response(response, resolved_model)
                # #3047 (c) — observation-only. Stamp this batch's retry-loop
                # altitude onto the result: ``attempt`` is 0-indexed, so the
                # (attempt+1)-th pass through the loop is the one that returned.
                # This is reyn's OWN retry count, not a raw wire count — equal to
                # delivered requests only while #3054's ``max_retries=0`` keeps
                # the SDK's internal retry at 0. ``_parse_response`` stays
                # attempt-agnostic (it only knows the response). Emitting the
                # audit-event is the op layer's job (P7: this provider never
                # imports op_runtime/ctx); the provider only returns the data.
                result["attempts"] = attempt + 1
                result["successful_batches"] = 1
                return result
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

    async def _aembedding_bounded(self, litellm: Any, **kwargs: Any) -> Any:
        """One ``litellm.aembedding`` call under the per-attempt bound (#3043).

        The single place the bound is applied, so a future call site cannot reach
        ``aembedding`` unbounded by forgetting to wrap it (the swept-missed shape
        that produced this bug). ``self._timeout is None`` = operator opted out
        (``embedding.timeout <= 0``) → the historical unbounded call, unchanged.

        ``max_retries=0`` (#3047, measured): without it, litellm's
        ``llms/openai/openai.py::OpenAIChatCompletion.embedding``'s
        ``max_retries = max_retries or litellm.DEFAULT_MAX_RETRIES`` turns our
        omitted kwarg (``None``) into ``2``, which litellm hands to the OpenAI
        SDK client as ``AsyncOpenAI(max_retries=2)`` — 1 initial + 2 SDK-internal
        retries = 3 HTTP requests PER attempt of the retry loop above, i.e. 9 on
        the wire for ``max_retries: 3``, all invisible to the ``attempt %d/%d``
        log line.

        ★ Passing ``max_retries=0`` as a kwarg ALONE does not fix this — ``0`` is
        just as falsy as ``None``, so ``0 or litellm.DEFAULT_MAX_RETRIES`` lands
        on ``2`` again (measured: still 9 requests). This is the SECOND ``x or
        DEFAULT`` trap in the same code path (the first is the one that made the
        omitted kwarg silently mean ``2`` in the first place) — passing our own
        falsy value straight into it changes nothing. The fix has to defeat the
        ``or`` itself: setting ``litellm.DEFAULT_MAX_RETRIES = 0`` makes the
        right-hand side of that ``or`` resolve to ``0`` too, so ANY falsy
        ``max_retries`` (ours, or a future omitted one) now correctly means "no
        SDK-level retry" instead of silently reviving 2. Audited for blast
        radius: reyn's chat-completion path (`llm.py`) always passes an
        explicit non-``None`` ``num_retries`` into ``litellm.acompletion``,
        which litellm maps to its own ``max_retries`` BEFORE this fallback would
        ever run (`main.py`: ``if num_retries is not None: max_retries =
        num_retries``) — so this global does not change chat's retry count, only
        embedding's, which is exactly the intended scope. Set once, permanently
        for the process (no per-call save/restore) — a temporal monkeypatch
        would race concurrent litellm calls sharing this event loop; a
        permanent process-wide 0 does not, because nothing in reyn ever relies
        on the ``2`` fallback firing.

        Passing ``max_retries=0`` explicitly as a kwarg too (redundant given the
        above, kept for self-documentation): reyn's own retry loop above is then
        the ONLY retry layer — 3 requests for 3 configured attempts, matching
        what the operator's config says. This is the SPENDING lever (vs.
        ``self._timeout``, the WAITING lever, above) — see #3047 for the
        measured 9->3 wire count. Does not touch cost-tracking's
        post-await-only recording (#3047 part b, separate follow-up).
        """
        litellm.DEFAULT_MAX_RETRIES = 0
        kwargs.setdefault("max_retries", 0)
        if self._timeout is None:
            return await litellm.aembedding(**kwargs)
        async with asyncio.timeout(self._timeout):
            return await litellm.aembedding(timeout=self._timeout, **kwargs)

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
