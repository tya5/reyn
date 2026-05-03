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

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        return self

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
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
