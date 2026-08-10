"""Tier 2: #3671 P4 item C-1 — `AgentRegistry.restore_all(only_names=...)`
defers building an in-flight agent's Session when it is NOT the caller's
requested target, so the target's own `attach()` is never gated on every
OTHER in-flight agent finishing first.

v2 (owner ruling, after lead-coder's review of the first version): the
original design left a NON-target agent un-resumed until something happened
to touch it (attach / delegation), possibly forever — a genuine
crash-recovery semantic change the owner rejected. The ruling: "resume
EVERY in-flight agent, just don't make the client's own startup wait for
it." `chat.py`'s `_background_attach` now calls
`registry.resume_deferred_agents()` right AFTER `attach(name)` succeeds — a
proactive sweep that finishes building+restoring+running every deferred
agent, matching pre-C-1 semantics (nothing is left un-resumed), just
ordered so the target agent is never delayed by it.

Two live paths can reach a deferred agent's `self._pending_restore` entry:
the proactive sweep above, AND `get_or_load`'s own on-demand hook (an
operator's own `/attach <other>`, or a live delegation's
`ensure_running(<other>)`, reaching it BEFORE the sweep does — genuinely
possible, since the client is already rendering while the sweep runs as its
own background task). Both are safe together (a single `dict.pop` gate,
plus each of `attach()`/`ensure_running()` independently guarding their own
run-task creation) — see the race-safety test below and `registry.py`'s
own `resume_deferred_agents` docstring for the full argument.

Durability is untouched by any of this: WAL replay (steps 2-3) and the
snapshot re-save to disk (step 4) are UNCONDITIONAL regardless of
`only_names` — only the Session-BUILD step is deferred. Witnessed here via
the PUBLIC `loaded_names()` surface (never a private `_`-prefixed accessor)
and via the real behavioral effect (a restored intervention actually
reaching the deferred agent's live intervention queue) — not just "no
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
async def test_deferred_agent_not_yet_resumed_right_after_attach(tmp_path, monkeypatch):
    """Tier 2: #3671 P4 item C-1 v2 (owner ruling: "resume every in-flight
    agent, just don't make the client's own startup wait for it") — ordering
    witness. Immediately after `restore_all(only_names={"alpha"})` returns,
    beta (deferred, non-target) is NOT YET built — proving the target's own
    attach genuinely never waited on beta. This is the FIRST half of the
    owner's ruling; the second half (beta eventually resumes too) is
    `test_resume_deferred_agents_resumes_every_deferred_agent` below."""
    monkeypatch.chdir(tmp_path)
    state_log = _seed_two_in_flight_agents(tmp_path)
    registry = _registry(tmp_path, state_log)

    await registry.restore_all(only_names={"alpha"})

    assert "alpha" in registry.loaded_names()
    assert "beta" not in registry.loaded_names(), (
        "beta must not be built as a side effect of restore_all() itself — "
        "only resume_deferred_agents() (called AFTER attach(), see chat.py) "
        "builds it, so the target's attach is never gated on it"
    )


@pytest.mark.asyncio
async def test_resume_deferred_agents_resumes_every_deferred_agent(tmp_path, monkeypatch):
    """Tier 2: #3671 P4 item C-1 v2 — the second half of the owner ruling.
    `resume_deferred_agents()` (called by `chat.py`'s `_background_attach`
    AFTER `attach(name)` succeeds) proactively finishes building+restoring+
    running every deferred agent — matching pre-C-1 crash-recovery semantics
    (every in-flight agent auto-resumes THIS run), just staggered so it
    never delays the target's own attach."""
    monkeypatch.chdir(tmp_path)
    state_log = _seed_two_in_flight_agents(tmp_path)
    registry = _registry(tmp_path, state_log)
    await registry.restore_all(only_names={"alpha"})
    assert "beta" not in registry.loaded_names()

    resumed = await registry.resume_deferred_agents()

    assert resumed == ["beta"]
    assert "beta" in registry.loaded_names()
    for _ in range(3):
        await asyncio.sleep(0)
    beta_session = registry.get_or_load("beta")
    iv_ids = [iv.id for iv in beta_session.interventions.list_active()]
    assert "iv_beta" in iv_ids, (
        f"beta's deferred intervention must be re-enqueued by the sweep; got {iv_ids}"
    )


@pytest.mark.asyncio
async def test_resume_deferred_agents_does_not_double_resume_a_racing_on_demand_attach(
    tmp_path, monkeypatch,
):
    """Tier 2: #3671 P4 item C-1 v2 (lead-coder review, #3683) — the
    interactive client is already rendering while `resume_deferred_agents()`
    runs as its own background task, so an operator's own `/attach beta` (or
    a live delegation's `ensure_running("beta")`) can reach a still-pending
    agent BEFORE the sweep gets to it. Simulated here by attaching to beta
    FIRST, then running the sweep — the sweep must not re-restore or spawn a
    second run task for it (both gated on the SAME `_pending_restore.pop`).

    ⚠️ This safety argument is SPECIFIC to asyncio's single-threaded
    cooperative scheduling — `dict.pop` only settles the race here because
    nothing else can run between `attach()`'s (or `resume_deferred_agents()`'s)
    pop and the synchronous `get_or_load` call that follows it in the SAME
    coroutine (no `await` in between). If `AgentRegistry` were ever made
    genuinely multi-threaded (real OS threads, not asyncio tasks), this test
    passing would NOT be evidence the race is still closed — `dict.pop` is
    atomic under the GIL for a single bytecode-level operation, but the
    surrounding "pop, then act on the popped value" sequence is not atomic
    across real threads the way it is across asyncio tasks. See
    `registry.py`'s own `resume_deferred_agents` docstring for the same
    caveat stated at the definition site."""
    monkeypatch.chdir(tmp_path)
    state_log = _seed_two_in_flight_agents(tmp_path)
    registry = _registry(tmp_path, state_log)
    await registry.restore_all(only_names={"alpha"})
    assert "beta" not in registry.loaded_names()

    beta_session = await registry.attach("beta")
    for _ in range(3):
        await asyncio.sleep(0)
    iv_ids_after_attach = [iv.id for iv in beta_session.interventions.list_active()]
    assert iv_ids_after_attach == ["iv_beta"], (
        f"on-demand attach must restore beta itself; got {iv_ids_after_attach}"
    )

    resumed = await registry.resume_deferred_agents()

    assert resumed == [], (
        "the sweep must find beta's pending entry already consumed by the "
        "on-demand attach — it must not re-list beta as something IT resumed"
    )
    # Not duplicated: the intervention list still has exactly one entry, not
    # two (which a second `restore_state` re-enqueuing the same iv WOULD NOT
    # by itself prove is safe — the real risk is a second RUN TASK, guarded
    # separately by attach()'s / ensure_running()'s own `_tasks` check; this
    # asserts the state-level symptom a double-restore/double-task race would
    # most visibly produce).
    iv_ids_after_sweep = [iv.id for iv in beta_session.interventions.list_active()]
    assert iv_ids_after_sweep == ["iv_beta"], (
        f"must not duplicate the restored intervention; got {iv_ids_after_sweep}"
    )


@pytest.mark.asyncio
async def test_resume_deferred_agents_is_a_noop_with_nothing_pending(tmp_path, monkeypatch):
    """Tier 2: calling `resume_deferred_agents()` when `only_names` covered
    every in-flight agent (or nothing was ever deferred) is a harmless no-op
    — the sweep must not raise or attempt to build anything."""
    monkeypatch.chdir(tmp_path)
    state_log = _seed_two_in_flight_agents(tmp_path)
    registry = _registry(tmp_path, state_log)
    await registry.restore_all()  # only_names=None — everyone eager already

    resumed = await registry.resume_deferred_agents()

    assert resumed == []


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
