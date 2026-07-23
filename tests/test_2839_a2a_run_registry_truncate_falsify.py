"""Tier 2: #2839 Phase 1 — A2A decouple recovery gate (CLAUDE.md truncate-falsify).

Phase 1 re-bases A2A's GetTask / Cancel / disposition surface off ``RunRegistry``
instead of the internal Task backend, and changes the SOURCE that
``routers/a2a.py`` reads for a run's resume state. CLAUDE.md's recovery-feature
PR gate applies: this test is the end-to-end witness that the decoupling does
NOT break A2A async-run resume — a pending-intervention (``input-required``) A2A
run must survive a restart + WAL truncation BELOW its source events and still
resume correctly.

This is not a new recovery source (the architect's firm settled that: Phase 1
adds no WAL-derived state — ``RunRegistry`` is already a standalone,
WAL-independent JSON snapshot, and intervention/answer state already lives in
Session's ``AgentSnapshot`` + WAL per #292 α, untouched by Phase 1). The two
halves of the division of labor are each proven to survive independently, and
then proven to compose correctly after restart:

  - ``RunRegistry`` (router + status mirror) — a standalone atomic-JSON
    snapshot; survives a restart on its own persistence, no WAL involved.
  - Session's ``AgentSnapshot`` + WAL (the actual resume-capable state: the
    outstanding intervention + the buffered answer) — survives WAL truncation
    below its OWN source events (the classic #2884/#2259 truncate-falsify
    shape), because the value is baked into the durable snapshot, not derived
    solely from the (now-dropped) WAL events.

Real ``Session`` / ``StateLog`` / ``AgentSnapshot`` / ``RunRegistry`` /
``A2AInterventionBus`` throughout — no mocks (CLAUDE.md mock ban). The only
stand-in is the LLM boundary, which this test never reaches (the intervention
is dispatched directly via ``Session.handle_intervention``, mirroring how
``tests/test_2884_hook_driven_turns_truncation_falsify.py`` isolates the WAL
mechanism from the router/LLM loop).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.interfaces.web.a2a_intervention import A2AInterventionBus
from reyn.interfaces.web.a2a_task_view import to_a2a_task
from reyn.interfaces.web.run_registry import RunRegistry, RunStatus
from reyn.runtime.session import Session
from reyn.user_intervention import InterventionAnswer, UserIntervention
from tests._support.agent_session import make_session

AGENT = "a2a-recovery-agent"


def _make_session(wal: Path, snapshot_path: Path) -> tuple[Session, StateLog]:
    state_log = StateLog(wal)
    session = make_session(agent_name=AGENT, state_log=state_log, snapshot_path=snapshot_path)
    # A listener must be registered or the origin-pin check parks the iv as
    # "stalled" instead of dispatching it (InterventionCoordinator.dispatch).
    session.register_intervention_listener("test-listener")
    return session, state_log


def _reconstruct_snapshot(agent_name: str, snapshot_path: Path, state_log: StateLog) -> AgentSnapshot:
    """Mirror ``AgentRegistry.restore_all``'s algorithm (same as #2884's
    ``_reconstruct``): load the durable snapshot, tail the WAL from its
    ``applied_seq``, replay onto it."""
    snap = AgentSnapshot.load(agent_name, snapshot_path)
    events = list(state_log.iter_from(snap.applied_seq))
    snap.apply_events(events)
    return snap


@pytest.mark.asyncio
async def test_pending_intervention_a2a_run_survives_restart_and_wal_truncation(tmp_path):
    """Tier 2: #2839 Phase 1 truncate-falsify (CLAUDE.md recovery-feature PR gate).

    Phase 1 (pre-crash): a real A2A async run creates a RunEntry, dispatches a
    genuine ask_user intervention through Session (landing in
    ``outstanding_interventions`` + the WAL ``intervention_dispatched`` event),
    and the A2A bus mirrors ``input-required`` onto the RunEntry.

    Truncation: filler WAL events push the truncation floor PAST the
    intervention's source event; ``truncate_below`` drops it (asserted gone
    from the raw file, not just counted).

    Phase 2 (restart): a FRESH Session (reconstructed snapshot + fresh WAL
    reader) AND a fresh RunRegistry (reloaded from its own persist_path,
    independent of the WAL) both come back up.

    Phase 3 (resume): the restored Session still has the intervention
    outstanding; answering it via ``Session.answer_pending_intervention`` (the
    real A2A answer-injection call) delivers correctly. The RunRegistry status
    mirror — read through ``to_a2a_task`` exactly as ``GET /a2a/tasks/{run_id}``
    would — shows ``input-required`` before the answer and ``working`` after,
    proving the decoupled GetTask/router surface stays coherent with the
    Session-owned resume state across the restart.

    RED if either half of the #292 α division of labor were broken by the
    decouple: a WAL-derived (not snapshot-backed) intervention would vanish at
    reconstruction; a RunRegistry rebuilt from a stale/dropped persist file
    would misreport status.
    """
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    run_registry_path = tmp_path / "run_registry.json"

    # ── Phase 1: pre-crash — real A2A run + real ask_user dispatch ─────────
    session, state_log = _make_session(wal, snapshot_path)
    run_registry = RunRegistry(persist_path=run_registry_path)
    entry = run_registry.create(
        agent_name=AGENT, chain_id="chain-recovery", session_id="a2a:ctx-recovery",
    )
    run_id = entry.run_id

    iv = UserIntervention(kind="ask_user", prompt="What is your name?", run_id=run_id)
    dispatch_task = asyncio.ensure_future(session.handle_intervention(iv))
    # Let dispatch proceed to the point of recording + awaiting the future.
    for _ in range(3):
        await asyncio.sleep(0)
    assert session.interventions.get(iv.id) is not None, (
        "sanity: the intervention must be genuinely dispatched (in the active "
        "queue) before we can prove it survives truncation"
    )

    # The A2A bus mirrors input-required onto the RunEntry (real object, same
    # method production wiring calls — issue #1981/#292 side-effect contract).
    bus = A2AInterventionBus(run_id, run_registry)
    await bus.on_dispatch(iv)
    assert run_registry.get(run_id).status == RunStatus.INPUT_REQUIRED
    assert to_a2a_task(run_registry.get(run_id))["status"]["state"] == "input-required"

    await session.journal.flush()  # drain the fire-and-forget WAL + snapshot writes

    # The source event (intervention_dispatched) is durable below this point.
    pre_truncate_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert any('"intervention_dispatched"' in line for line in pre_truncate_lines), (
        "sanity: the intervention_dispatched source event must be durable "
        "pre-truncation"
    )

    # ── Truncation: push the floor past the intervention's source event ────
    for i in range(150):
        await state_log.append("inbox_put", n=i)
    floor = state_log.current_seq - 5
    await state_log.truncate_below(floor)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= 1, (
        f"the intervention_dispatched source event must be truncated below "
        f"the floor; dropped={stats['dropped']}"
    )
    post_truncate_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert not any('"intervention_dispatched"' in line for line in post_truncate_lines), (
        "the source event must actually be gone from the WAL post-truncation "
        "(not just counted) — otherwise this test would pass vacuously even "
        "for a WAL-derived design"
    )

    await state_log.aclose()  # simulate the crash: tear down the WAL worker

    # ── Phase 2: restart — fresh Session AND fresh RunRegistry ─────────────
    session2, state_log2 = _make_session(wal, snapshot_path)
    reconstructed = _reconstruct_snapshot(AGENT, snapshot_path, state_log2)
    session2.restore_state(reconstructed)
    for _ in range(3):
        await asyncio.sleep(0)  # let the restored intervention task settle

    # RunRegistry is a standalone atomic-JSON snapshot (#2839 Phase 1 firm) —
    # a fresh instance reloads it independent of the WAL entirely.
    run_registry2 = RunRegistry(persist_path=run_registry_path)

    # ── Phase 3: resume — both halves survived, and compose correctly ──────
    assert session2.interventions.get(iv.id) is not None, (
        "the outstanding intervention must survive WAL truncation below its "
        "own source event (snapshot-backed via AgentSnapshot, not WAL-derived)"
    )
    restored_entry = run_registry2.get(run_id)
    assert restored_entry is not None
    assert restored_entry.status == RunStatus.INPUT_REQUIRED, (
        "RunRegistry's status mirror must survive a restart (its own standalone "
        "persistence — unaffected by the Session-side WAL truncation)"
    )
    assert to_a2a_task(restored_entry)["status"]["state"] == "input-required", (
        "GET /a2a/tasks/{run_id} (to_a2a_task) must still report input-required "
        "after restart — the decoupled GetTask surface stays coherent"
    )

    answer = InterventionAnswer(text="Alice")
    delivered = await session2.answer_pending_intervention(run_id, answer)
    assert delivered is True, (
        "answering the restored intervention (the real A2A answer-injection "
        "call, _handle_answer_injection's production path) must succeed"
    )

    # Mirror _handle_answer_injection's own RunRegistry status write on a
    # successful answer, proving the post-resume router/status mirror also
    # composes correctly.
    run_registry2.update(run_id, status="running")
    assert to_a2a_task(run_registry2.get(run_id))["status"]["state"] == "working", (
        "after a successful answer, the RunRegistry-backed GetTask surface "
        "must report working again — the full pending-intervention → resume "
        "→ router-coherent round trip survives restart + WAL truncation"
    )

    # Cleanup: let the restored dispatch task resolve.
    for _ in range(5):
        await asyncio.sleep(0)
    if not dispatch_task.done():
        dispatch_task.cancel()
    await state_log2.aclose()
