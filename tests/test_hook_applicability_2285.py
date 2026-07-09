"""Tier 2: #2285 increment 3 — session-scoped hook APPLICABILITY toggle + the 4th per-session layer.

set_hook_enabled(name, enabled) mutates a per-session disabled-set; the per-session HookDispatcher
skips a disabled hook at dispatch (live). Per-session by construction: each Session owns its own
dispatcher + disabled-set, so disabling a hook in S1 does NOT affect S2 — even though the hook
CONFIG is shared. _build_hook_registry gains a 4th per-session layer
(<session state dir>/hooks.yaml) so a hook can be defined at session scope. Real AgentRegistry +
real Sessions + real HookDispatcher.dispatch (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


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


def _write_agent_hook(tmp_path: Path, name: str) -> None:
    p = tmp_path / ".reyn" / "agents" / "alice" / "hooks.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump({"hooks": [
            {"on": "turn_end", "name": name, "template_push": {"message": "ping", "wake": True}},
        ]}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_hook_disable_is_per_session(tmp_path, monkeypatch):
    """Tier 2: CORE — disabling a shared hook in session S1 skips it for S1 but NOT S2 (per-session
    applicability despite shared config; each session's own dispatcher + disabled-set)."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    _write_agent_hook(tmp_path, "myhook")  # shared per-agent hook, before the sessions build registries
    reg.get_or_load("alice")
    s1 = reg.get_session("alice", await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None))
    s2 = reg.get_session("alice", await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None))

    s1.set_hook_enabled("myhook", False)  # disable in S1 only

    await s1._hook_dispatcher.dispatch("turn_end", {})
    await s2._hook_dispatcher.dispatch("turn_end", {})

    assert s1.inbox.qsize() == 0, "S1 disabled the hook → it did NOT fire"
    assert s2.inbox.qsize() >= 1, "S2 did not disable it → the same hook fired (per-session isolation)"


@pytest.mark.asyncio
async def test_hook_toggle_off_then_on(tmp_path, monkeypatch):
    """Tier 2: both directions — disable skips the hook at dispatch, enable restores firing."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    _write_agent_hook(tmp_path, "myhook")
    reg.get_or_load("alice")
    s = reg.get_session("alice", await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None))

    s.set_hook_enabled("myhook", False)
    await s._hook_dispatcher.dispatch("turn_end", {})
    assert s.inbox.qsize() == 0, "disabled → not fired"

    s.set_hook_enabled("myhook", True)
    await s._hook_dispatcher.dispatch("turn_end", {})
    assert s.inbox.qsize() >= 1, "re-enabled → fires again"


@pytest.mark.asyncio
async def test_per_session_hooks_yaml_is_a_4th_layer(tmp_path, monkeypatch):
    """Tier 2: a hook defined in the per-session hooks.yaml (4th layer) appears in THIS session's
    merged registry with scope 'per-session' — session-scoped hook definition."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    s = reg.get_session("alice", await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None))

    # write a per-session hook at this session's state dir, then re-build the registry
    per_session = Path(s._snapshot_path).parent / "hooks.yaml"
    per_session.write_text(
        yaml.safe_dump({"hooks": [
            {"on": "turn_start", "name": "sesshook", "template_push": {"message": "x", "wake": True}},
        ]}),
        encoding="utf-8",
    )
    await s._reapply_hooks({})  # re-read all layers + swap the registry

    state = {h["name"]: h for h in s.hook_state()}
    assert "sesshook" in state, "the per-session hook is in the merged set"
    assert state["sesshook"]["scope"] == "per-session"
    assert state["sesshook"]["enabled"] is True


def test_hook_slash_is_registered():
    """Tier 2: /hook is registered on import (status-bar can dispatch it)."""
    from reyn.interfaces.slash import REGISTRY
    assert REGISTRY.get("hook") is not None


@pytest.mark.asyncio
async def test_hook_slash_disables_via_public_state(tmp_path, monkeypatch):
    """Tier 2: /hook off <name> disables the hook (reflected in the public hook_state); malformed
    args → reply_error, no change."""
    from reyn.interfaces.slash.hook import hook_cmd

    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    _write_agent_hook(tmp_path, "myhook")
    reg.get_or_load("alice")
    s = reg.get_session("alice", await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None))
    s.is_attached = True

    await hook_cmd(s, "off myhook")
    assert {h["name"]: h["enabled"] for h in s.hook_state()}.get("myhook") is False

    await hook_cmd(s, "on myhook")
    assert {h["name"]: h["enabled"] for h in s.hook_state()}.get("myhook") is True

    await hook_cmd(s, "garbage")  # malformed → no crash, no change
    assert {h["name"]: h["enabled"] for h in s.hook_state()}.get("myhook") is True
