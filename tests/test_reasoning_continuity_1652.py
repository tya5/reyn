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
from reyn.chat.router_system_prompt import build_system_prompt
from reyn.config import ReasoningConfig, _build_chat_config

_SP_BASE = dict(
    agent_name="chat",
    agent_role="r",
    available_skills=[],
    available_agents=[],
    memory_index={"status": "not_found", "content": ""},
)


# ── Tier 1: config schema + reyn.yaml loader ────────────────────────────────


def test_reasoning_config_defaults_all_on_n3():
    """Tier 1: #1652 — defaults: continuity ON, display ON, recent_turns=3."""
    c = ReasoningConfig()
    assert (c.continuity, c.display, c.recent_turns) == (True, True, 3)


def test_chat_reasoning_loads_from_yaml_nondefault():
    """Tier 1: #1652 — chat.reasoning round-trips NON-DEFAULT values from
    reyn.yaml (the loader wires the field, not just the dataclass)."""
    c = _build_chat_config({"reasoning": {"continuity": False, "recent_turns": 7}})
    assert c.reasoning.continuity is False
    assert c.reasoning.recent_turns == 7
    assert c.reasoning.display is True  # unspecified → default


def test_chat_reasoning_parsed_without_compaction_block():
    """Tier 1: #1652 — a chat: block with only reasoning (no compaction) still
    honours reasoning (guards the early-return path in _build_chat_config)."""
    c = _build_chat_config({"reasoning": {"display": False}})
    assert c.reasoning.display is False


# ── Tier 1: build_system_prompt injection (omit-when-empty) ─────────────────


def test_sp_omits_empty_reasoning_section():
    """Tier 1: #1652 — empty section → SP byte-identical to no-section (omit-
    when-empty, LLMReplay-safe, same as act_turn_reasoning)."""
    assert build_system_prompt(**_SP_BASE, reasoning_continuity_section="") == build_system_prompt(**_SP_BASE)


def test_sp_injects_reasoning_section_when_present():
    """Tier 1: #1652 — a non-empty section is injected into the SP."""
    out = build_system_prompt(**_SP_BASE, reasoning_continuity_section="MARKER_REASONING_X")
    assert "MARKER_REASONING_X" in out


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
