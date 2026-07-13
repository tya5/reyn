"""
Token usage tracking and cost estimation.

Cost calculation delegates entirely to litellm's built-in pricing database —
no pricing table is maintained here. litellm is updated with each release
to reflect current provider prices.

The pricing info litellm uses is snapshotted at run time and stored in the
eval result JSON so past runs can be audited accurately.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Prompt-cache metrics (#1772). Both are SUBSETS / side-metrics of the
    # billed tokens, NOT additive to total_tokens:
    #   cached_tokens          — prompt tokens served from cache (cache READ /
    #                            hit). A subset of prompt_tokens; cross-provider
    #                            normalized (litellm surfaces this as both
    #                            usage.cache_read_input_tokens and
    #                            usage.prompt_tokens_details.cached_tokens).
    #   cache_creation_tokens  — tokens written to the cache (Anthropic
    #                            cache-write); 0 for providers without an
    #                            explicit write metric (OpenAI / Gemini).
    cached_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        return self

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TokenUsage":
        """Reconstruct from a dict produced by ``to_dict``.

        Resilient to missing fields AND to a malformed value (defaults to 0) —
        useful for backward-compat reading of older / hand-edited / corrupted
        WAL records. ``.get(key, 0)`` only defaults a *missing* key, so a key
        present with ``null`` or a non-numeric value would otherwise crash
        (``int(None)`` → TypeError, ``int("abc")`` → ValueError); ``_coerce_int``
        closes that gap to honour the stated resilience contract.
        """
        def _coerce_int(v: object) -> int:
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        return cls(
            prompt_tokens=_coerce_int(data.get("prompt_tokens", 0)),
            completion_tokens=_coerce_int(data.get("completion_tokens", 0)),
            cached_tokens=_coerce_int(data.get("cached_tokens", 0)),
            cache_creation_tokens=_coerce_int(data.get("cache_creation_tokens", 0)),
        )


def _usage_object_for(usage: TokenUsage):
    """Build a ``litellm.types.utils.Usage`` carrying the cache breakdown so
    ``litellm.cost_per_token`` prices cached tokens at the cache rate instead
    of the full input rate.

    Provider-convention note: reyn's ``TokenUsage.prompt_tokens`` ALREADY
    INCLUDES ``cached_tokens`` and ``cache_creation_tokens`` as subsets — this
    is litellm's own normalized ``response.usage.prompt_tokens`` value (see
    ``_extract_usage`` in ``llm.py``; litellm's Anthropic transformation adds
    both cache figures into ``prompt_tokens`` before returning the response
    object, matching the OpenAI convention where ``prompt_tokens_details.
    cached_tokens`` is a subset of ``prompt_tokens``). Passing the breakdown
    via ``prompt_tokens_details`` (OpenAI-style) rather than the top-level
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` kwargs is
    deliberate: litellm's ``cost_per_token`` treats the latter as "Anthropic
    style" (prompt_tokens EXCLUDES cache) and RE-ADDS them to prompt_tokens
    before costing — which would double-count here, since our prompt_tokens
    already includes them. The ``prompt_tokens_details`` path takes
    prompt_tokens at face value and subtracts cache tokens internally
    (``text_tokens = prompt_tokens - cache_hit - cache_creation``), which is
    the correct convention for reyn's already-normalized TokenUsage.
    """
    from litellm.types.utils import Usage

    return Usage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        prompt_tokens_details={
            "cached_tokens": usage.cached_tokens,
            "cache_creation_tokens": usage.cache_creation_tokens,
        },
    )


def estimate_cost(
    model: str,
    usage: TokenUsage,
) -> tuple[float | None, dict | None]:
    """
    Estimate cost in USD using litellm's pricing database — cache-aware
    (#cache-cost-accuracy): cached (cache-read) prompt tokens are priced at
    the model's discounted cache-read rate and cache-creation (cache-write)
    tokens at the cache-creation rate, not the full input rate, by passing
    the cache breakdown to ``litellm.cost_per_token`` (see
    ``_usage_object_for`` for the provider-convention this relies on).

    Returns (cost_usd, pricing_snapshot).
    pricing_snapshot records the per-token prices litellm used at this moment
    — store it in run results so past runs remain auditable after price changes.
    Returns (None, None) if litellm does not know the model.
    """
    if usage.total_tokens == 0:
        return 0.0, None

    try:
        import litellm

        # #1829 S2 (option 3): a ``litellm.model_cost`` entry can EXIST with None
        # prices — a price-less PLACEHOLDER (e.g. a ``litellm.Router`` deployment
        # registration adds one). ``cost_per_token`` then returns 0.0 for it,
        # conflating "cost unknown" with "free". An unpriced model means cost
        # UNKNOWN → return (None, None), not 0.0. (unknown ≠ free — None is the
        # explicit unknown sentinel; 0.0 would wrongly read as a real free call.)
        _entry = litellm.model_cost.get(model)
        if (not _entry
                or _entry.get("input_cost_per_token") is None
                or _entry.get("output_cost_per_token") is None):
            return None, None

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            usage_object=_usage_object_for(usage),
        )
        total_cost = prompt_cost + completion_cost

        # Snapshot the per-token prices litellm has for this model
        entry = litellm.model_cost.get(model, {})
        input_per_token = entry.get("input_cost_per_token")
        output_per_token = entry.get("output_cost_per_token")

        try:
            from importlib.metadata import version as _pkg_version
            litellm_version = _pkg_version("litellm")
        except Exception:
            litellm_version = "unknown"

        snapshot = {
            "model": model,
            "prompt_per_1m_usd": round(input_per_token * 1_000_000, 6) if input_per_token else None,
            "completion_per_1m_usd": round(output_per_token * 1_000_000, 6) if output_per_token else None,
            "source": "litellm",
            "litellm_version": litellm_version,
        }
        return total_cost, snapshot

    except Exception:
        return None, None


@dataclass
class CostBreakdown:
    """Cache-aware cost breakdown for one LLM call (or an aggregation across
    many, via ``__add__`` / ``__iadd__``) — the 4 components the cost panel
    is expected to render (#cache-cost-accuracy), plus the SAVINGS figures the
    owner flagged as first-class for the panel's savings block.

    ``prompt_cost`` covers only the NON-cached portion of the prompt (see
    ``estimate_cost_breakdown`` for the exact token split). ``total_cost`` is
    the sum of all 4 cost components — decisively pinned equal to
    ``estimate_cost``'s litellm-derived total by the accuracy test
    (components-sum-to-total invariant).

    Savings surface (backend-only; no panel UI yet):
      - ``cache_savings`` — how many USD the cached (cache-read) tokens saved
        vs paying the full input rate for them:
        ``cached_tokens × (input_rate − cache_read_rate)``. A dollar amount,
        so it aggregates additively like the cost components.
      - ``cache_hit_rate`` — token-level cache hit rate,
        ``cached_tokens / prompt_tokens`` (0.0 when ``prompt_tokens`` is 0 —
        divide-by-zero guarded). Derived from the aggregatable
        ``prompt_tokens`` / ``cached_tokens`` counters so the ratio stays
        correct across a multi-turn aggregation (summing per-turn ratios
        would be wrong — summing the underlying token counts is not).
    """
    prompt_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_creation_cost: float = 0.0
    completion_cost: float = 0.0
    # Savings block inputs (aggregatable). ``cache_savings`` is a USD amount;
    # ``prompt_tokens`` / ``cached_tokens`` back the derived ``cache_hit_rate``.
    cache_savings: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_cost(self) -> float:
        return (
            self.prompt_cost
            + self.cache_read_cost
            + self.cache_creation_cost
            + self.completion_cost
        )

    @property
    def cache_hit_rate(self) -> float:
        """Token-level cache hit rate ``cached_tokens / prompt_tokens``; 0.0
        when ``prompt_tokens`` is 0 (divide-by-zero guard)."""
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens

    def __add__(self, other: "CostBreakdown") -> "CostBreakdown":
        return CostBreakdown(
            prompt_cost=self.prompt_cost + other.prompt_cost,
            cache_read_cost=self.cache_read_cost + other.cache_read_cost,
            cache_creation_cost=self.cache_creation_cost + other.cache_creation_cost,
            completion_cost=self.completion_cost + other.completion_cost,
            cache_savings=self.cache_savings + other.cache_savings,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )

    def __iadd__(self, other: "CostBreakdown") -> "CostBreakdown":
        self.prompt_cost += other.prompt_cost
        self.cache_read_cost += other.cache_read_cost
        self.cache_creation_cost += other.cache_creation_cost
        self.completion_cost += other.completion_cost
        self.cache_savings += other.cache_savings
        self.prompt_tokens += other.prompt_tokens
        self.cached_tokens += other.cached_tokens
        return self

    def to_dict(self) -> dict:
        return {
            "prompt_cost": self.prompt_cost,
            "cache_read_cost": self.cache_read_cost,
            "cache_creation_cost": self.cache_creation_cost,
            "completion_cost": self.completion_cost,
            "total_cost": self.total_cost,
            "cache_savings": self.cache_savings,
            "cache_hit_rate": self.cache_hit_rate,
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
        }


def estimate_cost_breakdown(
    model: str,
    usage: TokenUsage,
) -> "CostBreakdown | None":
    """Cache-aware cost BREAKDOWN into 4 components: prompt (non-cached input)
    / cache-read (cache-hit discount) / cache-creation (cache-write surcharge)
    / completion (output), plus the savings figures (``cache_savings`` USD +
    ``cache_hit_rate``) for the panel's savings block. Data-only — no
    rendering; the cost panel reads this to display a breakdown instead of
    just the total.

    Token-split convention (mirrors litellm's own ``generic_cost_per_token``,
    verified empirically — see ``_usage_object_for``'s docstring for why):
    ``TokenUsage.prompt_tokens`` already INCLUDES ``cached_tokens`` and
    ``cache_creation_tokens`` as subsets, so the non-cached ("regular")
    portion priced at the full input rate is
    ``prompt_tokens - cached_tokens - cache_creation_tokens`` (floored at 0).

    Cache-rate fallback: when a model has no explicit
    ``cache_read_input_token_cost`` / ``cache_creation_input_token_cost``
    entry, litellm itself defaults that rate to ``0.0`` (NOT the input rate —
    verified against ``litellm.litellm_core_utils.llm_cost_calc.utils.
    _get_cost_per_unit``'s ``default_value=0.0``); this function does the same
    so the components-sum-to-total invariant holds against litellm's own
    total for every model, cache-capable or not.

    Note: does not replicate litellm's >200k-token TIERED pricing threshold
    (rare for reyn's usage) — the accuracy test stays below that threshold.

    Returns ``None`` for an unpriced/unknown model (mirrors ``estimate_cost``'s
    None-sentinel — unknown ≠ free, #1829).
    """
    if usage.total_tokens == 0:
        return CostBreakdown()

    try:
        import litellm

        entry = litellm.model_cost.get(model)
        if (not entry
                or entry.get("input_cost_per_token") is None
                or entry.get("output_cost_per_token") is None):
            return None

        input_rate = float(entry["input_cost_per_token"])
        output_rate = float(entry["output_cost_per_token"])
        cache_read_rate = float(entry.get("cache_read_input_token_cost") or 0.0)
        cache_creation_rate = float(entry.get("cache_creation_input_token_cost") or 0.0)

        regular_prompt_tokens = max(
            usage.prompt_tokens - usage.cached_tokens - usage.cache_creation_tokens,
            0,
        )

        # Savings: what the cached tokens would have cost at the full input
        # rate MINUS what they actually cost at the cache-read rate.
        cache_savings = usage.cached_tokens * (input_rate - cache_read_rate)

        return CostBreakdown(
            prompt_cost=regular_prompt_tokens * input_rate,
            cache_read_cost=usage.cached_tokens * cache_read_rate,
            cache_creation_cost=usage.cache_creation_tokens * cache_creation_rate,
            completion_cost=usage.completion_tokens * output_rate,
            cache_savings=cache_savings,
            prompt_tokens=usage.prompt_tokens,
            cached_tokens=usage.cached_tokens,
        )
    except Exception:
        return None
