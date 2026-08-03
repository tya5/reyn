"""Tier 2: #3671 P4 item C-1 — `AgentRegistry.restore_all(only_names=...)`
defers building+running an in-flight agent's Session when it is NOT the
caller's requested target, applying the deferred restore lazily at first
real use (`attach()` or a delegation target's `ensure_running()` — both
route through `get_or_load`) instead of never.

Before this fix, `restore_all()` built (`get_or_load` + `restore_state` +
`ensure_running`, a full Session construction plus a live running task) EVERY
in-flight agent unconditionally — proportional to agent count, all on
whatever critical path called `restore_all()` (P2 already moved that off the
RENDER path via `chat.py`'s background task, but the requested agent's own
`attach()` still had to wait for every OTHER in-flight agent to finish
building first).

Durability is untouched by this: WAL replay (steps 2-3) and the snapshot
re-save to disk (step 4) are UNCONDITIONAL regardless of `only_names` — only
the Session-BUILD step is deferred. Witnessed here via the PUBLIC
`loaded_names()` surface (never a private `_`-prefixed accessor) and via the
real behavioral effect (a restored intervention actually reaching the
deferred agent's live intervention queue once it IS reached) — not just "no
exception raised".

Real `AgentRegistry` + real `Session` throughout (mirrors
`test_intervention_restore.py`'s seeding pattern — pre-write a snapshot.json
with a stranded intervention, the same L4/L5 fixture shape) — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session


def _snapshot_with_intervention(*, agent_name: str, iv_id: str) -> AgentSnapshot:
    snap = AgentSnapshot.empty(agent_name)
    snap.outstanding_interventions[iv_id] = {
        "kind": "ask_user", "prompt": "Q?", "detail": "", "choices": [],
        "suggestions": [], "run_id": "rA", "actor": "demo", "id": iv_id,
    }
    snap.applied_seq = 5
    return snap


def _seed_two_in_flight_agents(tmp_path: Path) -> StateLog:
    """Seeds `alpha` and `beta`, each with a stranded intervention on disk
    (no WAL entries needed — restore_all's step 1 loads snapshot.json
    directly, same as test_intervention_restore.py)."""
    agents_dir = tmp_path / ".reyn" / "agents"
    for name in ("alpha", "beta"):
        agent_dir = agents_dir / name
        state_dir = agent_dir / "state"
        state_dir.mkdir(parents=True)
        AgentProfile.new(name, role="").save(agent_dir)
        snap = _snapshot_with_intervention(agent_name=name, iv_id=f"iv_{name}")
        snap.save(state_dir / "snapshot.json")
    return StateLog(tmp_path / ".reyn" / "wal.jsonl")


def _registry(tmp_path: Path, state_log: StateLog) -> AgentRegistry:
    def _factory(profile: AgentProfile):
        s = make_session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    return AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )


@pytest.mark.asyncio
async def test_only_names_defers_the_non_target_agents_session_build(tmp_path, monkeypatch):
    """Tier 2: #3671 P4 item C-1's core witness — `restore_all(only_names=
    {"alpha"})` builds alpha's session (the requested target) but NOT beta's
    (in-flight but not requested), observed via the PUBLIC `loaded_names()`."""
    monkeypatch.chdir(tmp_path)
    state_log = _seed_two_in_flight_agents(tmp_path)
    registry = _registry(tmp_path, state_log)

    snapshots = await registry.restore_all(only_names={"alpha"})

    assert "alpha" in snapshots and "beta" in snapshots, (
        "WAL replay / snapshot re-save (steps 1-4) must still cover BOTH "
        "agents regardless of only_names — durability is unaffected"
    )
    assert "alpha" in registry.loaded_names(), (
        "the requested agent must still be built+running immediately"
    )
    assert "beta" not in registry.loaded_names(), (
        "a non-requested in-flight agent must NOT be built during restore_all "
        "— its build is deferred to first real use, not eliminated"
    )


@pytest.mark.asyncio
async def test_deferred_agent_is_restored_on_first_attach(tmp_path, monkeypatch):
    """Tier 2: the deferred agent is NOT lost — attaching to it later applies
    the SAME restore_state it would have gotten eagerly (the stranded
    intervention reaches its live queue), proving `get_or_load`'s hook
    actually fires exactly where `attach()` reaches it."""
    monkeypatch.chdir(tmp_path)
    state_log = _seed_two_in_flight_agents(tmp_path)
    registry = _registry(tmp_path, state_log)
    await registry.restore_all(only_names={"alpha"})
    assert "beta" not in registry.loaded_names()

    beta_session = await registry.attach("beta")
    for _ in range(3):
        await asyncio.sleep(0)

    iv_ids = [iv.id for iv in beta_session.interventions.list_active()]
    assert "iv_beta" in iv_ids, (
        f"beta's deferred intervention must be re-enqueued once attached; got {iv_ids}"
    )


@pytest.mark.asyncio
async def test_deferred_agent_is_restored_via_delegation_ensure_running(tmp_path, monkeypatch):
    """Tier 2: the OTHER "first real use" trigger — a delegation target
    reached via `ensure_running()` (never an explicit `/attach`) — also gets
    its deferred restore applied, since `ensure_running` calls `get_or_load`
    internally too."""
    monkeypatch.chdir(tmp_path)
    state_log = _seed_two_in_flight_agents(tmp_path)
    registry = _registry(tmp_path, state_log)
    await registry.restore_all(only_names={"alpha"})
    assert "beta" not in registry.loaded_names()

    beta_session = await registry.ensure_running("beta")
    for _ in range(3):
        await asyncio.sleep(0)

    iv_ids = [iv.id for iv in beta_session.interventions.list_active()]
    assert "iv_beta" in iv_ids


@pytest.mark.asyncio
async def test_restore_all_without_only_names_still_builds_every_in_flight_agent(tmp_path, monkeypatch):
    """Tier 2: regression guard — `restore_all()` with NO `only_names` (e.g.
    `mcp.py`, which must serve any agent name) keeps the original, fully
    eager behavior byte-identically: every in-flight agent is built
    immediately, none deferred."""
    monkeypatch.chdir(tmp_path)
    state_log = _seed_two_in_flight_agents(tmp_path)
    registry = _registry(tmp_path, state_log)

    await registry.restore_all()

    assert "alpha" in registry.loaded_names()
    assert "beta" in registry.loaded_names(), (
        "with only_names=None every in-flight agent must still be built "
        "eagerly — this is a real behavioral requirement for mcp.py, not "
        "just a compat default"
    )
