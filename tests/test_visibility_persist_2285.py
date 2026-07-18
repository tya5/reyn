"""Tier 2: #2285 step2 — persist/restore the session visibility + hook toggles (SEPARATE store).

The visibility override persists to ``<session state dir>/visibility.yaml`` and the hook disabled-set
to ``hooks.yaml``'s ``disabled:`` list — both DISTINCT from ``config.yaml`` (the spawner-narrowing
capability floor = part of the authorized envelope). On a fresh session over the same state dir
(= restart), ``load_persisted_toggles`` restores them + composes the override ON TOP of the
authoritative envelope (re-resolved from ``config.yaml``). The CRITICAL invariant carried across
restart: a persisted override can NEVER re-widen past the floor — persist-hiding a spawner-DENIED
tool then toggling it ON after reload STILL denies it (``visible ⊆ authorized`` survives persist +
reload; the override never touches the floor). Real AgentRegistry + a SECOND registry over the same
root reconstructing the (alice, sid) session via ``spawn_session(sid=sid)`` = the real restart path
(registry.py ``restore_all`` line 999). No mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
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
    if not (tmp_path / ".reyn" / "agents" / "alice").exists():
        AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


def _allows_tool(session: Session, name: str) -> bool:
    return ContextualLayer(session.contextual_permission).allows(CapabilityAxis.TOOL, name)


def _write_agent_hook(tmp_path: Path, name: str) -> None:
    p = tmp_path / ".reyn" / "agents" / "alice" / "hooks.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump({"hooks": [
            {"on": "turn_end", "name": name, "template_push": {"message": "ping", "wake": True}},
        ]}),
        encoding="utf-8",
    )


def _reload_spawned(tmp_path: Path, sid: str) -> Session:
    """Simulate a restart: a FRESH registry over the same root reconstructs the (alice, sid) spawned
    session via ``spawn_session(sid=sid)`` — the exact restart path (registry.py ``restore_all``
    line 999), which re-keys the per-session dir + fires ``load_persisted_toggles``. The config.yaml
    floor + visibility.yaml override both persist on disk, so the reload re-derives them."""
    reg2 = _make_registry(tmp_path)
    reg2.get_or_load("alice")
    reg2.spawn_session("alice", sid=sid, presentation_consumer=None, intervention_bridge=None)
    return reg2.get_session("alice", sid)


@pytest.mark.asyncio
async def test_persisted_override_cannot_rewiden_spawner_floor_after_reload(tmp_path, monkeypatch):
    """Tier 2: CRITICAL — a persisted visibility override NEVER re-widens past the spawner floor.
    Persist-hide a spawner-DENIED tool → reload (fresh registry over the same root) → toggle it ON →
    STILL denied. The reloaded override composes atop the re-resolved envelope (config.yaml floor), so
    visible ⊆ authorized survives persist + reload. RED if load restored the override as an authority
    over the floor, or if the persist wrote into config.yaml (dropping the floor's deny)."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", narrowing={"tool_deny": ["delete_file"]}, presentation_consumer=None, intervention_bridge=None)
    session = reg.get_session("alice", sid)
    assert _allows_tool(session, "delete_file") is False  # spawner floor denies it

    # persist-hide the (already floor-denied) tool → writes visibility.yaml alongside config.yaml
    session.set_capability_visible("tool", "delete_file", False)

    # reload: a fresh registry reconstructs from disk (config.yaml floor + visibility.yaml override)
    reloaded = _reload_spawned(tmp_path, sid)
    assert _allows_tool(reloaded, "delete_file") is False, "floor still denies after reload"

    # toggle it ON after reload → the override discards it, but the floor STILL denies it
    reloaded.set_capability_visible("tool", "delete_file", True)
    assert _allows_tool(reloaded, "delete_file") is False, (
        "persisted override MUST NOT re-widen past the spawner floor after reload "
        "(visible ⊆ authorized survives persist + reload)"
    )


@pytest.mark.asyncio
async def test_visibility_override_round_trips_across_reload(tmp_path, monkeypatch):
    """Tier 2: a toggle-OFF on an envelope-ALLOWED tool survives restart — hidden after reload, and
    toggle-ON after reload restores it. Round-trips a non-default override value."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None)  # no narrowing → envelope allows all tools
    session = reg.get_session("alice", sid)
    assert _allows_tool(session, "read_file") is True

    session.set_capability_visible("tool", "read_file", False)  # hide + persist

    reloaded = _reload_spawned(tmp_path, sid)
    assert _allows_tool(reloaded, "read_file") is False, "toggle-OFF survives reload (persisted)"

    reloaded.set_capability_visible("tool", "read_file", True)
    assert _allows_tool(reloaded, "read_file") is True, "toggle-ON after reload restores it"


@pytest.mark.asyncio
async def test_hidden_set_round_trips_exact_value(tmp_path, monkeypatch):
    """Tier 2: the exact hidden-set (a non-default multi-entry value) round-trips through the store —
    the reloaded session reports the same hidden_by_session as before restart."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None)
    session = reg.get_session("alice", sid)
    session.set_capability_visible("tool", "read_file", False)
    session.set_capability_visible("tool", "ask_user", False)
    before = sorted(
        tuple(sorted(i.items())) for i in session.capability_visibility_state()["hidden_by_session"]
    )

    reloaded = _reload_spawned(tmp_path, sid)
    after = sorted(
        tuple(sorted(i.items())) for i in reloaded.capability_visibility_state()["hidden_by_session"]
    )
    assert after == before, "the exact hidden-set round-trips through visibility.yaml"
    assert {"kind": "tool", "name": "read_file"} in reloaded.capability_visibility_state()["hidden_by_session"]


@pytest.mark.asyncio
async def test_hook_disabled_set_survives_reload(tmp_path, monkeypatch):
    """Tier 2: a disabled hook survives restart — disable a shared per-agent hook, reload, the
    reloaded session still reports it disabled (the disabled-set persisted to hooks.yaml's
    ``disabled:`` list, distinct from any session-defined ``hooks:``)."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    _write_agent_hook(tmp_path, "myhook")  # shared per-agent hook (built into every session registry)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None)
    session = reg.get_session("alice", sid)
    assert any(h["name"] == "myhook" and h["enabled"] for h in session.hook_state())

    session.set_hook_enabled("myhook", False)  # disable + persist to hooks.yaml disabled:

    reloaded = _reload_spawned(tmp_path, sid)
    states = {h["name"]: h["enabled"] for h in reloaded.hook_state()}
    assert states.get("myhook") is False, \
        "disabled hook survives reload (persisted to hooks.yaml `disabled:`)"


@pytest.mark.asyncio
async def test_clearing_override_removes_persisted_store(tmp_path, monkeypatch):
    """Tier 2: toggling the last override back ON leaves no stale persisted hidden-set — a reload
    after re-enabling everything restores an empty override (the store is pruned, not left dirty)."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", presentation_consumer=None, intervention_bridge=None)
    session = reg.get_session("alice", sid)
    session.set_capability_visible("tool", "read_file", False)
    session.set_capability_visible("tool", "read_file", True)  # back to default → store pruned

    reloaded = _reload_spawned(tmp_path, sid)
    assert reloaded.capability_visibility_state()["hidden_by_session"] == [], \
        "no stale persisted override after clearing it"
    assert _allows_tool(reloaded, "read_file") is True
