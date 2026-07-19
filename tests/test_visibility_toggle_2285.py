"""Tier 2: #2285 step1 — session-scoped LLM tool-VISIBILITY toggle, visible ⊆ authorized.

set_capability_visible(kind, name, visible) mutates a per-session override and re-applies it live:
CapabilityVisibility.reapply_visibility_override RE-RESOLVES the agent envelope from base (topology ∩
delegate ∩ per-session config via resolved_profile_for) and composes the override as ONE MORE
restrict-only ∩ conjunct, then SETs the live tool gate (contextual_permission / excluded_categories,
#3121 step3 Extract Class -- owned by CapabilityVisibility, exposed on Session via the public
``contextual_permission`` property). The core
security invariant: a toggle can only HIDE within the authorized envelope — toggle-ON cannot revive
a capability the envelope denies (re-resolve-from-base stops at the envelope). Real AgentRegistry +
spawn_session_recorded(narrowing=...) sets a REAL envelope (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.security.permissions.effective import CapabilityAxis, ContextualLayer
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


def _allows_tool(session: Session, name: str) -> bool:
    return ContextualLayer(session.contextual_permission).allows(CapabilityAxis.TOOL, name)


@pytest.mark.asyncio
async def test_toggle_on_cannot_revive_envelope_denied_tool(tmp_path, monkeypatch):
    """Tier 2: CORE security invariant (visible ⊆ authorized) — a tool the agent ENVELOPE denies
    cannot be made visible by set_capability_visible(True). RED if _reapply appended/union'd instead
    of re-resolving from base; GREEN because it re-resolves the envelope (which still denies it)."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", narrowing={"tool_deny": ["delete_file"]}, presentation_consumer=None, intervention_bridge=None)
    session = reg.get_session("alice", sid)

    assert _allows_tool(session, "delete_file") is False  # envelope denies it

    session.set_capability_visible("tool", "delete_file", True)  # try to reveal an unauthorized tool

    assert _allows_tool(session, "delete_file") is False, \
        "toggle-ON MUST NOT revive an envelope-denied capability (visible ⊆ authorized)"


@pytest.mark.asyncio
async def test_toggle_hides_then_restores_within_envelope(tmp_path, monkeypatch):
    """Tier 2: both directions within the envelope — an envelope-ALLOWED tool: OFF hides it (next
    turn), ON restores it. The re-widen the union-based apply_per_session_narrowing couldn't do."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None)  # no narrowing → envelope allows all tools
    session = reg.get_session("alice", sid)
    assert _allows_tool(session, "delete_file") is True  # allowed by the envelope

    session.set_capability_visible("tool", "delete_file", False)
    assert _allows_tool(session, "delete_file") is False, "toggle-OFF hides it"

    session.set_capability_visible("tool", "delete_file", True)
    assert _allows_tool(session, "delete_file") is True, "toggle-ON restores it (up to the envelope)"


@pytest.mark.asyncio
async def test_visibility_state_reflects_envelope_and_override(tmp_path, monkeypatch):
    """Tier 2: capability_visibility_state — authorized excludes the envelope-denied tool (never
    togglable); hidden_by_session carries the override."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", narrowing={"tool_deny": ["delete_file"]}, presentation_consumer=None, intervention_bridge=None)
    session = reg.get_session("alice", sid)
    session.set_capability_visible("tool", "ask_user", False)

    state = session.capability_visibility_state()
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}
    assert "delete_file" not in authorized_tools, "envelope-denied tool absent from authorized"
    assert "ask_user" in authorized_tools, "an allowed tool is authorized (togglable)"
    assert {"kind": "tool", "name": "ask_user"} in state["hidden_by_session"]
