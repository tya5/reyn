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

        Resilient to missing fields (defaults to 0) — useful for
        backward-compat reading of older WAL records.
        """
        return cls(
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            cached_tokens=int(data.get("cached_tokens", 0)),
            cache_creation_tokens=int(data.get("cache_creation_tokens", 0)),
        )


def estimate_cost(
    model: str,
    usage: TokenUsage,
) -> tuple[float | None, dict | None]:
    """
    Estimate cost in USD using litellm's pricing database.

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
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
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
