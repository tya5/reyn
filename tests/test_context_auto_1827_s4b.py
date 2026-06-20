"""Tier 2: context-auto per-turn compose (#1827 S4b).

When untrusted external content is live in the active context (a history entry
carrying the #1862 ``external_source`` marker), the agent's per-turn contextual
narrowing composes the minimal ``_untrusted`` profile with the static topology
narrowing (most-restrictive). The taint is derived from the active history, so it
**self-clears** once the marked entry compacts out (until-compaction scope).
Untrusted absent → the static contextual (byte-identical).

These pin ``Session._effective_contextual_for_turn`` (the per-turn callback the
RouterLoopDriver consults) — and that the composed contextual actually DENIES the
dangerous tools at the shared gate (the same ``tool_contextually_denied`` the live
RouterLoop / control-IR gates call), so the narrowing is enforced, not cosmetic.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from reyn.security.permissions.effective import (
    ContextualPermission,
    tool_contextually_denied,
)


def _session(tmp_path: Path, *, contextual=None) -> Session:
    s = Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        contextual_permission=contextual,
    )
    return s


def _mark_untrusted(s: Session) -> None:
    s.history.append(ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True}))


def test_untainted_returns_static(tmp_path):
    """Tier 2: with no untrusted entry the per-turn contextual is the static one."""
    s = _session(tmp_path)  # no static narrowing
    eff = s._effective_contextual_for_turn()
    assert eff is None  # byte-identical
    s.history.append(ChatMessage(role="user", content="normal", meta={}))
    eff = s._effective_contextual_for_turn()
    assert eff is None


def test_tainted_composes_untrusted_and_denies(tmp_path):
    """Tier 2: an untrusted entry → the per-turn contextual denies the dangerous
    tools (the built-in _untrusted deny-set) at the shared gate."""
    s = _session(tmp_path)
    _mark_untrusted(s)
    eff = s._effective_contextual_for_turn()
    assert eff is not None
    # the dangerous side-effecting surfaces are now denied (context-auto)
    for denied in ("memory_operation__remember_shared", "delegate_to_agent",
                   "exec__sandboxed_exec", "sandboxed_exec"):
        assert tool_contextually_denied(eff, denied), denied
    # a read tool stays allowed
    assert not tool_contextually_denied(eff, "recall")


def test_self_clears_when_taint_removed(tmp_path):
    """Tier 2: once the untrusted entry is gone (compaction), the narrowing clears."""
    s = _session(tmp_path)
    _mark_untrusted(s)
    eff = s._effective_contextual_for_turn()
    assert tool_contextually_denied(eff, "exec__sandboxed_exec")
    # simulate the untrusted entry compacting out of the active context
    s.history = [m for m in s.history if not (m.meta or {}).get("external_source")]
    eff = s._effective_contextual_for_turn()
    assert eff is None  # back to static (none)


def test_composes_with_static_union(tmp_path):
    """Tier 2: a static topology narrowing AND the untrusted profile both apply
    while tainted (union-of-excludes / most-restrictive)."""
    static = ContextualPermission(tool_deny=frozenset({"web__search"}))
    s = _session(tmp_path, contextual=static)
    # untainted: only the static deny applies
    eff = s._effective_contextual_for_turn()
    assert tool_contextually_denied(eff, "web__search")
    assert not tool_contextually_denied(eff, "exec__sandboxed_exec")
    # tainted: BOTH the static deny AND the untrusted deny-set apply
    _mark_untrusted(s)
    eff = s._effective_contextual_for_turn()
    assert tool_contextually_denied(eff, "web__search")          # static
    assert tool_contextually_denied(eff, "exec__sandboxed_exec")  # untrusted
