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
from reyn.runtime.session_params import CapabilityScope
from reyn.security.permissions.effective import (
    ContextualPermission,
    tool_contextually_denied,
)
from tests._support.agent_session import make_session
from tests._support.untrusted_narrowing import narrowing_on


def _session(tmp_path: Path, *, contextual=None) -> Session:
    # #3501: the narrowing is opt-in, so a test whose subject IS the narrowing
    # must turn it on. ``test_off_by_default_no_narrowing`` below is the arm that
    # deliberately does not.
    s = make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(contextual_permission=contextual),
        safety=narrowing_on(),
    )
    return s


def _mark_untrusted(s: Session) -> None:
    """#5276: goes through ``_append_history`` — the real mutation
    chokepoint that maintains ``Session._untrusted_taint_active``
    incrementally — not a bare ``s.history.append``, which the
    incremental hook never observes."""
    s._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )


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
    for denied in ("remember_shared", "run_prompt",
                   "send_to_session", "exec"):
        assert tool_contextually_denied(eff, denied), denied
    # a read tool stays allowed
    assert not tool_contextually_denied(eff, "web_fetch")


def test_self_clears_when_taint_removed(tmp_path):
    """Tier 2: once the untrusted entry is gone (compaction), the narrowing clears."""
    s = _session(tmp_path)
    _mark_untrusted(s)
    eff = s._effective_contextual_for_turn()
    assert tool_contextually_denied(eff, "exec")
    # #5276: simulate the untrusted entry compacting out of the active
    # context via a real compaction watermark advance (a role="summary"
    # entry through _append_history, the actual production mechanism —
    # see test_3380_tool_tab_ephemeral_narrowing.py's own
    # test_narrowing_self_clears_when_a_real_compaction_covers_the_taint
    # for the same shape), not a raw self.history reassignment the
    # incremental taint hook never observes.
    tainted_seq = next(
        m.seq for m in s.history if (m.meta or {}).get("external_source")
    )
    s._append_history(
        ChatMessage(
            role="summary", content="summarised",
            meta={
                "structured": {}, "covers_from_seq": 1,
                "covers_through_seq": tainted_seq,
            },
        )
    )
    eff = s._effective_contextual_for_turn()
    assert eff is None  # back to static (none)


def test_composes_with_static_union(tmp_path):
    """Tier 2: a static topology narrowing AND the untrusted profile both apply
    while tainted (union-of-excludes / most-restrictive)."""
    static = ContextualPermission(tool_deny=frozenset({"web_search"}))
    s = _session(tmp_path, contextual=static)
    # untainted: only the static deny applies
    eff = s._effective_contextual_for_turn()
    assert tool_contextually_denied(eff, "web_search")
    assert not tool_contextually_denied(eff, "exec")
    # tainted: BOTH the static deny AND the untrusted deny-set apply
    _mark_untrusted(s)
    eff = s._effective_contextual_for_turn()
    assert tool_contextually_denied(eff, "web_search")          # static
    assert tool_contextually_denied(eff, "exec")  # untrusted


def test_off_by_default_no_narrowing_even_when_tainted(tmp_path):
    """Tier 2: #3501 — with the default config an untrusted entry narrows NOTHING.

    The counterpart to every arm above: the same taint, the same session, but no
    ``safety.threat_scan.capability_narrowing`` opt-in. This is the arm that goes
    RED if the opt-in gate is removed from ``_ephemeral_contextual_for_turn``.
    """
    s = make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    _mark_untrusted(s)
    eff = s._effective_contextual_for_turn()
    assert eff is None
    assert not tool_contextually_denied(eff, "exec")


def test_off_by_default_keeps_a_static_narrowing_intact(tmp_path):
    """Tier 2: #3501 — opting OUT of the untrusted narrowing does not weaken the
    static envelope. The two are separate narrowings; the opt-in governs only the
    context-auto one."""
    static = ContextualPermission(tool_deny=frozenset({"web_search"}))
    s = make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(contextual_permission=static),
    )
    _mark_untrusted(s)
    eff = s._effective_contextual_for_turn()
    assert tool_contextually_denied(eff, "web_search")   # envelope survives
    assert not tool_contextually_denied(eff, "exec")     # untrusted term absent
