"""Tier 2: CLI summary formatter — format_tokens_and_cost pure function contracts.

`format_tokens_and_cost` is the single source of truth for how token usage and
cost are formatted for both `reyn run` and `reyn eval` output.  It has two
branches (cost present / absent) and uses comma-formatted numbers.  Pinning it
prevents a silent format change from breaking the CLI's cost reporting surface.
"""
from __future__ import annotations

from reyn.interfaces.cli.summary import format_tokens_and_cost
from reyn.llm.pricing import TokenUsage


def _usage(prompt: int, completion: int) -> TokenUsage:
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion)


def test_format_includes_prompt_and_completion() -> None:
    """Tier 2: output contains both prompt and completion token counts."""
    out = format_tokens_and_cost(_usage(100, 50), None)
    assert "100" in out
    assert "50" in out


def test_format_total_is_sum() -> None:
    """Tier 2: total_tokens = prompt + completion surfaces in output."""
    out = format_tokens_and_cost(_usage(100, 50), None)
    assert "150" in out


def test_format_with_no_cost_omits_dollar_sign() -> None:
    """Tier 2: cost_usd=None → no cost suffix in output."""
    out = format_tokens_and_cost(_usage(100, 50), None)
    assert "$" not in out


def test_format_with_zero_cost_omits_dollar_sign() -> None:
    """Tier 2: cost_usd=0 (falsy cost) → no cost suffix in output."""
    out = format_tokens_and_cost(_usage(100, 50), 0.0)
    assert "$" not in out


def test_format_with_positive_cost_includes_cost() -> None:
    """Tier 2: positive cost_usd → '~$N.NNNN' suffix included."""
    out = format_tokens_and_cost(_usage(100, 50), 0.0125)
    assert "$" in out
    assert "0.0125" in out


def test_format_comma_separates_large_numbers() -> None:
    """Tier 2: token counts ≥ 1000 use comma formatting."""
    out = format_tokens_and_cost(_usage(10_000, 5_000), None)
    assert "10,000" in out
    assert "5,000" in out
    assert "15,000" in out
