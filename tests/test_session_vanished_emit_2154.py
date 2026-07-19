"""Tier 2: OS invariant — #2154 session_vanished emit + reconstruction symmetry.

session_vanished is a registered WAL kind whose doc claims it's emitted, but
remove_session never appended it (the create↔destroy asymmetry tui-coder mined:
session_spawned IS appended, session_vanished was not). Two-part fix:

1. EMIT, cause-parameterized: remove_session is the shared teardown seam for BOTH
   the genuine vanish (ephemeral auto-vanish / explicit removal → MUST record) and
   the rewind-reconstruction drop (_drop_session → must NOT record: a drop undoes
   history, recording it would pollute the append-only WAL + corrupt as-of-cut
   reconstruction). _drop_session passes record=False; genuine callers default to
   record=True. The destroy-side mirror of session_spawned.

2. RECONSTRUCTION symmetry: _materialize_rewind consumes session_vanished
   (latest-≤-cut-wins) so a session vanished at-or-before the cut reconstructs as
   GONE. A genuine vanish normally rmtree's the dir (discovery won't surface it), so
   this guard is load-bearing when a dir SURVIVES its vanish (crash mid-rmtree, or a
   future session re-materialise seam).

Real AgentRegistry + StateLog + Session (no mocks), mirroring the production
scoped_session_factory (registry threaded into each Session).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import rewind
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log,
                    registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


def _vanished_sids(log: StateLog, name: str) -> list[str]:
    return [
        e.get("sid") for e in log.iter_from(0)
        if e.get("kind") == "session_vanished" and e.get("name") == name
    ]


@pytest.mark.asyncio
async def test_genuine_ephemeral_vanish_emits_session_vanished(tmp_path):
    """Tier 2: the genuine ephemeral auto-vanish emits session_vanished (the create↔
    destroy WAL symmetry — was the missing half). Falsifies the doc-claimed-but-never-
    emitted defect."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", mode="ephemeral", presentation_consumer=None, intervention_bridge=None)

    eph = reg._peek_session("alice", sid)
    eph._maybe_schedule_ephemeral_vanish()
    if eph._vanish_task is not None:
        await eph._vanish_task

    # #2279: session_vanished is a FIRE-AND-FORGET WAL append (async-decoupled durability, #2259) —
    # drain the worker before the raw WAL read so the presence assert is deterministic (await'ing
    # the vanish task does not guarantee its WAL append is durable).
    await reg.state_log.flush()
    assert sid in _vanished_sids(reg.state_log, "alice")  # destroy recorded


@pytest.mark.asyncio
async def test_reconstruction_drop_does_not_emit_session_vanished(tmp_path):
    """Tier 2: the rewind-reconstruction drop (_drop_session) does NOT emit
    session_vanished — a drop undoes history; recording it would pollute the WAL and
    corrupt as-of-cut reconstruction (the cause-separation, record=False)."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice", mode="persistent", presentation_consumer=None, intervention_bridge=None)
    assert sid not in _vanished_sids(reg.state_log, "alice")  # spawn alone: none

    await reg._drop_session("alice", sid)

    assert _vanished_sids(reg.state_log, "alice") == []  # reconstruction: no WAL pollution


@pytest.mark.asyncio
async def test_session_vanished_before_cut_reconstructs_gone(tmp_path):
    """Tier 2: reconstruction symmetry — a session whose session_vanished is at-or-
    before the cut reconstructs as GONE even though it SURVIVES in the registry (the
    record landed but the teardown didn't complete: crash mid-rmtree / future
    re-materialise). The destroy-side mirror of the spawn-cut; latest-≤-cut-wins."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    log = reg.state_log
    sid = await reg.spawn_session_recorded("alice", mode="ephemeral", presentation_consumer=None, intervention_bridge=None)  # spawn ≤ cut, live
    assert sid in reg.session_ids("alice")                    # present (public surface)
    # the vanish was RECORDED but the session survives (teardown didn't finish).
    await log.append("session_vanished", entity_kind="session", name="alice", sid=sid)

    # cut at/after the vanish → gone as-of-cut → reconstruction drops it.
    await reg._materialize_rewind(
        reconstruct_seq=log.current_seq, workspace_at_or_below=log.current_seq,
    )

    assert sid not in reg.session_ids("alice")                # reconstructs gone


@pytest.mark.asyncio
async def test_session_vanished_after_cut_survives_reconstruction(tmp_path):
    """Tier 2: the cut boundary — a session whose session_vanished is in the ABANDONED
    interval (N < V < R) still existed as-of-cut, so reconstruction must NOT drop it.

    ``is_active_seq(V)`` is False for V in the abandoned interval → ``vanished_by_cut``
    is False → session is reconstructed (not dropped).  The vanish is on the discarded
    branch; the session was alive as-of-target N.
    """
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    log = reg.state_log
    sid = await reg.spawn_session_recorded("alice", mode="persistent", presentation_consumer=None, intervention_bridge=None)
    cut = log.current_seq                                          # cut = N (after spawn)
    await log.append("session_vanished", entity_kind="session",
                     name="alice", sid=sid)                        # V = cut+1 (abandoned)
    R = await rewind(log, target_n=cut)                            # R = cut+2; V in (N, R)

    await reg._materialize_rewind(reconstruct_seq=R, workspace_at_or_below=cut)

    assert sid in reg.session_ids("alice")                         # existed as-of-cut → kept
