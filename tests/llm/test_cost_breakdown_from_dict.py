"""Tier 1: CostBreakdown.from_dict — #5771 stage②'s own wire decode side.

Added so ``project_remote_snapshot`` (read_model.py) can reconstruct a real
``CostBreakdown`` from the dict ``project_status`` (agui/state.py) put on the
wire via ``.to_dict()`` — reusing the ONE existing serialization, never a
second one (architect's explicit instruction on #5771). Mirrors
``TokenUsage.from_dict``'s own resilience contract and its own test file
(``test_token_usage_from_dict.py``) — the same "missing/null/non-numeric
value defaults instead of raising" shape, verified independently here rather
than assumed to transfer.
"""
from __future__ import annotations

from reyn.llm.pricing import CostBreakdown


def test_valid_record_round_trips() -> None:
    """Tier 1: a well-formed record reconstructs the same real fields
    (falsification: an over-aggressive coercion would lose the real
    figures)."""
    original = CostBreakdown(
        prompt_cost=0.012,
        cache_read_cost=0.003,
        cache_creation_cost=0.001,
        completion_cost=0.045,
        cache_savings=0.007,
        prompt_tokens=1200,
        cached_tokens=340,
    )
    restored = CostBreakdown.from_dict(original.to_dict())
    assert restored.prompt_cost == 0.012
    assert restored.cache_read_cost == 0.003
    assert restored.cache_creation_cost == 0.001
    assert restored.completion_cost == 0.045
    assert restored.cache_savings == 0.007
    assert restored.prompt_tokens == 1200
    assert restored.cached_tokens == 340
    # Derived properties re-compute correctly from the restored fields alone
    # — they are never read back from to_dict()'s own precomputed values.
    assert restored.total_cost == original.total_cost
    assert restored.cache_hit_rate == original.cache_hit_rate


def test_derived_properties_in_the_dict_are_not_read_back_as_fields() -> None:
    """Tier 1: to_dict() includes total_cost/cache_hit_rate for a reader
    that only wants the finished figures — from_dict must not try to
    accept them as constructor fields (they would silently diverge from
    the property the moment either the dict or the object changed
    independently)."""
    data = {
        "prompt_cost": 1.0, "cache_read_cost": 0.0, "cache_creation_cost": 0.0,
        "completion_cost": 0.0, "cache_savings": 0.0, "prompt_tokens": 10,
        "cached_tokens": 0,
        # A deliberately WRONG total_cost/cache_hit_rate — from_dict must
        # ignore these, not adopt them.
        "total_cost": 999.0, "cache_hit_rate": 999.0,
    }
    restored = CostBreakdown.from_dict(data)
    assert restored.total_cost == 1.0
    assert restored.cache_hit_rate == 0.0


def test_missing_keys_default_to_the_dataclass_defaults() -> None:
    """Tier 1: an empty dict reconstructs the all-zero default — the
    pre-existing missing-key resilience TokenUsage.from_dict already
    established, verified independently for this class."""
    restored = CostBreakdown.from_dict({})
    assert restored == CostBreakdown()


def test_null_value_defaults_instead_of_raising() -> None:
    """Tier 1: a key present with null defaults rather than raising
    (mirrors TokenUsage.from_dict's own null-value fix, #4844-adjacent
    bug-mining pattern) — a malformed/older wire payload must not crash
    the remote read model."""
    restored = CostBreakdown.from_dict({"prompt_cost": None, "prompt_tokens": None})
    assert restored.prompt_cost == 0.0
    assert restored.prompt_tokens == 0


def test_non_numeric_value_defaults_instead_of_raising() -> None:
    """Tier 1: a non-numeric string defaults rather than raising."""
    restored = CostBreakdown.from_dict({"completion_cost": "abc", "cached_tokens": "xyz"})
    assert restored.completion_cost == 0.0
    assert restored.cached_tokens == 0
