"""Tier 2: TokenUsage.from_dict is resilient to malformed WAL values.

Found via bug-mining (2026-06-20). `from_dict`'s docstring promises resilience
"for backward-compat reading of older WAL records", but `int(data.get(k, 0))`
only defaults a *missing* key — a key present with `null` or a non-numeric
string crashed (`int(None)` → TypeError, `int("abc")` → ValueError). A corrupted
/ hand-edited / older-format WAL record (read at `llm_call_recorder.py:651`)
would then crash on replay.

Fix: `_coerce_int` defaults a malformed value to 0, honouring the stated
resilience contract.

Falsification: pre-fix the null / non-numeric cases raised; the round-trip test
proves good data is unaffected.
"""
from __future__ import annotations

from reyn.llm.pricing import TokenUsage


def test_null_value_defaults_to_zero() -> None:
    """Tier 2: a key present with null defaults to 0 (was TypeError)."""
    u = TokenUsage.from_dict({"prompt_tokens": None, "completion_tokens": None})
    assert u.prompt_tokens == 0
    assert u.completion_tokens == 0
    assert u.total_tokens == 0


def test_non_numeric_value_defaults_to_zero() -> None:
    """Tier 2: a non-numeric string defaults to 0 (was ValueError)."""
    u = TokenUsage.from_dict({"prompt_tokens": "abc"})
    assert u.prompt_tokens == 0


def test_missing_keys_still_default_to_zero() -> None:
    """Tier 2: the pre-existing missing-key resilience is preserved."""
    u = TokenUsage.from_dict({})
    assert u.total_tokens == 0


def test_valid_record_round_trips() -> None:
    """Tier 2: a well-formed record reconstructs unchanged (regression guard).

    Falsification: if the coercion were too aggressive (e.g. always 0), this
    would lose the real counts.
    """
    original = TokenUsage(
        prompt_tokens=120,
        completion_tokens=34,
        cached_tokens=80,
        cache_creation_tokens=12,
    )
    restored = TokenUsage.from_dict(original.to_dict())
    assert restored.prompt_tokens == 120
    assert restored.completion_tokens == 34
    assert restored.cached_tokens == 80
    assert restored.cache_creation_tokens == 12
    assert restored.total_tokens == 154
