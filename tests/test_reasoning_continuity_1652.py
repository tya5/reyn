"""#1652: cross-turn reasoning-continuity bounding + render primitives.

Tier 1: pure config-default-independent contract of the bound/render helpers
(the knob's semantics + the omit-when-empty render). The persist/replay/gating
behavior lands with that wiring once the config schema locks.
"""
from __future__ import annotations

from reyn.chat.reasoning_continuity import (
    UNBOUNDED,
    bound_reasoning,
    render_reasoning_section,
)


def test_bound_keeps_most_recent_n():
    """Tier 1: #1652 — a positive bound keeps the last N (most recent)."""
    items = ["r1", "r2", "r3", "r4", "r5"]
    assert bound_reasoning(items, 3) == ["r3", "r4", "r5"]


def test_bound_unbounded_sentinel_keeps_all():
    """Tier 1: #1652 — the unbounded sentinel (<=0) keeps all entries (the
    'always-send-all' option)."""
    items = ["r1", "r2", "r3"]
    assert bound_reasoning(items, UNBOUNDED) == items
    assert bound_reasoning(items, -1) == items


def test_bound_n_larger_than_list_keeps_all():
    """Tier 1: #1652 — N larger than the list returns the whole list (no pad)."""
    assert bound_reasoning(["r1", "r2"], 10) == ["r1", "r2"]


def test_render_empty_is_empty_string():
    """Tier 1: #1652 — no reasoning → empty string, so the system prompt stays
    byte-identical to the no-continuity shape (LLMReplay-safe, mirrors #1212)."""
    assert render_reasoning_section([]) == ""


def test_render_includes_all_items_most_recent_last():
    """Tier 1: #1652 — the section carries every passed entry, in order
    (most recent last), under the continuity header."""
    out = render_reasoning_section(["older thought", "newer thought"])
    assert "prior_reasoning" in out
    assert "older thought" in out and "newer thought" in out
    assert out.index("older thought") < out.index("newer thought")
    # context-not-instruction framing carried (mirrors act_turn_reasoning intent)
    assert "context, not an instruction" in out
