"""Tier 2: #2884 — the hook-driven-turns loop-valve counter TRUNCATION-falsify.

``Session._hook_driven_turns`` (the #1800 slice 7 loop-valve, bounding hook
self-continuation) was an in-memory-only int: a crash+restart reset it to 0,
handing a near-cap self-wake loop a free fresh budget window (a
defense-in-depth gap, not an acute exploit — the composer self-loop state
that would actually re-arm the dangerous chain is separately non-durable;
see architect's design comment on #2884).

The fix snapshots the counter (``AgentSnapshot.hook_driven_turns``), mirroring
``buffered_intervention_answers``. A pure-WAL-derived count is explicitly
REJECTED by the design: consumed WAL entries are pruned by ``truncate_below``
(only ``REWIND_KIND`` is force-kept) — exactly the #2259 config-loss class.
This test proves the snapshot-backed value survives truncation of its OWN
source WAL events (the CLAUDE.md recovery-feature PR gate), mirroring
``tests/test_2259_config_truncation_bug.py``.

Real Session / StateLog / AgentSnapshot (no mocks); only the LLM boundary
(``_loop_driver.run_turn``) is replaced with a plain async recorder, exactly
as ``tests/test_hook_loop_valve_1800_7.py`` does to isolate the valve.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

AGENT = "valve-agent"


def _make_session(wal: Path, snapshot_path: Path, *, cap: int = 100) -> tuple[Session, StateLog]:
    safety = SafetyConfig(
        loop=LoopConfig(max_hook_driven_turns=cap),
        on_limit=OnLimitConfig(mode="unattended"),
    )
    state_log = StateLog(wal)
    session = make_session(
        agent_name=AGENT, state_log=state_log, snapshot_path=snapshot_path, safety=safety,
    )
    return session, state_log


def _fake_run_turn(session: Session) -> None:
    """Replace the LLM boundary with a no-op recorder (isolates the valve)."""
    async def _noop(user_text: str, chain_id: str) -> None:
        return None
    session._loop_driver.run_turn = _noop  # type: ignore[method-assign]


async def _push_hook(session: Session, text: str) -> None:
    await session._put_inbox("hook", {"name": "turn_end", "text": text, "wake": True})


def _reconstruct(agent_name: str, snapshot_path: Path, state_log: StateLog) -> AgentSnapshot:
    """Mirror ``AgentRegistry.restore_all``'s algorithm: load the durable snapshot,
    tail the WAL from its ``applied_seq``, replay onto it."""
    snap = AgentSnapshot.load(agent_name, snapshot_path)
    events = list(state_log.iter_from(snap.applied_seq))
    snap.apply_events(events)
    return snap


@pytest.mark.asyncio
async def test_hook_driven_turns_survives_wal_truncation_below_its_source_events(tmp_path):
    """Tier 2: #2884 truncate-falsify (CLAUDE.md recovery-feature PR gate). Drive N=3
    hook-driven turns → their ``hook_driven_turns_set`` WAL source events + the durable
    snapshot both land with count=3. Filler events push the truncation floor PAST those
    source events; ``truncate_below`` drops them (asserted). Reconstructing (load snapshot
    + replay the WAL tail) still yields 3 — because the value is baked into the durable
    FULL-STATE snapshot, not derived solely from the (now-dropped) WAL events. RED if the
    snapshot field / restore wiring were absent: reconstruction would see 0."""
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path, cap=100)
    _fake_run_turn(session)

    N = 3
    for i in range(N):
        await _push_hook(session, f"h{i}")
        await session.run_one_iteration()
    await session.journal.flush()  # drain the fire-and-forget WAL + snapshot writes

    assert session.hook_driven_turns == N, "sanity: the live counter advanced to N"

    # the source events (one hook_driven_turns_set per turn) are durable below this point.
    pre_truncate_lines = [
        line for line in wal.read_text().splitlines() if line.strip()
    ]
    assert any(
        '"hook_driven_turns_set"' in line and f'"count": {N}' in line
        for line in pre_truncate_lines
    ), "sanity: the final hook_driven_turns_set@N source event must be durable pre-truncation"

    # push filler events far past the counter's source events, then truncate below them.
    for i in range(150):
        await state_log.append("inbox_put", n=i)
    floor = state_log.current_seq - 5
    await state_log.truncate_below(floor)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= N, (
        f"the {N} hook_driven_turns_set source events (and other early entries) must be "
        f"truncated below the floor; dropped={stats['dropped']}"
    )
    post_truncate_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert not any('"hook_driven_turns_set"' in line for line in post_truncate_lines), (
        "the source events must actually be gone from the WAL post-truncation (not just "
        "counted) — otherwise this test would pass vacuously even for a WAL-derived design"
    )

    await state_log.aclose()  # simulate the crash: tear down run1's WAL worker

    # reconstruct (simulates a restart): a FRESH StateLog + Session over the SAME wal/snapshot
    # (mirrors AgentRegistry.restore_all: load snapshot, tail WAL from applied_seq, replay, restore).
    session2, state_log2 = _make_session(wal, snapshot_path, cap=100)
    reconstructed = _reconstruct(AGENT, snapshot_path, state_log2)
    session2.restore_state(reconstructed)

    assert session2.hook_driven_turns == N, (
        f"the loop-valve counter must survive WAL truncation below its own source events "
        f"(snapshot-backed, not WAL-derived); got {session2.hook_driven_turns}, expected {N}"
    )

    await state_log2.aclose()


@pytest.mark.asyncio
async def test_agent_snapshot_hook_driven_turns_round_trip(tmp_path):
    """Tier 1: a basic AgentSnapshot save/load round-trip preserves a non-zero
    ``hook_driven_turns`` value (not the 0 default — proves the field is actually
    threaded through serialize/save/load, not silently dropped)."""
    path = tmp_path / "snapshot.json"
    snap = AgentSnapshot(agent_name=AGENT, hook_driven_turns=7)
    snap.save(path)

    loaded = AgentSnapshot.load(AGENT, path)

    assert loaded.hook_driven_turns == 7, (
        f"hook_driven_turns must round-trip through save/load; got {loaded.hook_driven_turns}"
    )
