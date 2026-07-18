"""Tier 2: #2285 increment 2 — the /visibility slash dispatches to set_capability_visible.

`/visibility on|off <kind> <name>` is the status-bar row → Session API seam. It maps 1:1 to
Session.set_capability_visible, preserving visible ⊆ authorized (the mechanism enforces it; the
slash is a thin parse). Real AgentRegistry + Session (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash import REGISTRY
from reyn.interfaces.slash.visibility import visibility_cmd
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.security.permissions.effective import CapabilityAxis, ContextualLayer


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


def _allows(session: Session, name: str) -> bool:
    return ContextualLayer(session.contextual_permission).allows(CapabilityAxis.TOOL, name)


async def _session(tmp_path) -> Session:
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None)
    session = reg.get_session("alice", sid)
    session.is_attached = True
    return session


def test_visibility_command_is_registered():
    """Tier 2: the /visibility command is registered on import (status-bar can dispatch it)."""
    assert REGISTRY.get("visibility") is not None


@pytest.mark.asyncio
async def test_slash_off_then_on_toggles_capability(tmp_path, monkeypatch):
    """Tier 2: `/visibility off tool <name>` hides it, `/visibility on tool <name>` restores it —
    the slash drives set_capability_visible, so the live gate changes both directions."""
    monkeypatch.chdir(tmp_path)
    session = await _session(tmp_path)
    assert _allows(session, "delete_file") is True  # envelope allows

    await visibility_cmd(session, "off tool delete_file")
    assert _allows(session, "delete_file") is False, "/visibility off hides it"

    await visibility_cmd(session, "on tool delete_file")
    assert _allows(session, "delete_file") is True, "/visibility on restores it"


@pytest.mark.asyncio
async def test_slash_bad_args_no_crash_no_state_change(tmp_path, monkeypatch):
    """Tier 2: malformed args → reply_error, no exception, no visibility change."""
    monkeypatch.chdir(tmp_path)
    session = await _session(tmp_path)
    await visibility_cmd(session, "garbage")            # wrong arity
    await visibility_cmd(session, "maybe tool x")       # bad on/off
    await visibility_cmd(session, "off widget x")       # bad kind
    assert session.capability_visibility_state()["hidden_by_session"] == [], "no toggle on malformed args"
