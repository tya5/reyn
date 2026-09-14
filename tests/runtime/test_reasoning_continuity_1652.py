"""#1652: cross-turn reasoning-continuity config schema.

Tier 1: pure config-default-independent contract of the ``ReasoningConfig``
schema + reyn.yaml loader. The persist/replay/gating behavior is exercised
in ``test_reasoning_integration_1652.py``; the native wire re-attach in
``test_reasoning_integration_1652.py``'s wire-reattach tests.

#6182: the PRIOR text-section-append primitives this file once tested
(``bound_reasoning``, ``render_reasoning_section``, and
``build_system_prompt``'s ``reasoning_continuity_section`` parameter) were
retired and removed — 0 production callers once #1652/② moved replay to the
native wire re-attach (``RouterHistoryBuffer``).
"""
from __future__ import annotations

from reyn.config import ReasoningConfig, _build_chat_config

# ── Tier 1: config schema + reyn.yaml loader ────────────────────────────────


def test_reasoning_config_defaults_all_on_unbounded():
    """Tier 1: #6179 stage ⑴ — defaults: continuity ON, display ON,
    recent_turns=0 (unbounded). Owner ruling (2026-09-14, verbatim):
    "わたしは直近3つと言ったことはないと思うあなたが勝手に絞ってる。すべて
    おくるべきだし、spill 対象にするのが自然だと思うな" — the prior
    default of 3 was this module's own invention. #6179 stage ⑵ (#6180)
    made spill the shrink-flow's own bound on reasoning's size, so this
    knob no longer needs to pre-emptively truncate by default."""
    c = ReasoningConfig()
    assert (c.continuity, c.display, c.recent_turns) == (True, True, 0)


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
