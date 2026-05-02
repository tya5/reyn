"""
RunSummary: token + cost reporter shared by `run` and `eval`.

Single source of truth for how token usage and cost are formatted on the CLI.
"""
from __future__ import annotations
from reyn.llm.pricing import TokenUsage


def format_tokens_and_cost(usage: TokenUsage, cost_usd: float | None) -> str:
    cost_str = f"  ~${cost_usd:.4f}" if cost_usd is not None and cost_usd > 0 else ""
    return (
        f"tokens: {usage.prompt_tokens:,} prompt + {usage.completion_tokens:,} completion"
        f" = {usage.total_tokens:,} total{cost_str}"
    )


def print_run_result(token_usage: TokenUsage | None, cost_usd: float | None) -> None:
    """Single-run summary line printed by `reyn run`."""
    if token_usage is None:
        return
    print(f"\n{format_tokens_and_cost(token_usage, cost_usd)}")


def print_eval_total(total_tokens: TokenUsage, total_cost_usd: float) -> None:
    """Aggregate summary line printed by `reyn eval` after all cases."""
    if total_tokens.total_tokens == 0:
        return
    cost = total_cost_usd if total_cost_usd > 0 else None
    print(f" {format_tokens_and_cost(total_tokens, cost)}")
