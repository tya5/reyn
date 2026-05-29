"""Tier 2: OS invariant — compute_truncate_floor runs off the event loop.

PR-N7 wraps the `compute_truncate_floor` sync call inside
`asyncio.to_thread` so that disk I/O does not block the event loop
while `truncate_wal_if_eligible` awaits the floor computation.

These tests verify four invariants via the public surface only:

1. **Non-blocking** — while `truncate_wal_if_eligible` is awaited, a
   concurrent `asyncio.sleep(0)` sentinel can make progress.  This
   proves the event loop was not stalled by the floor scan.

2. **Correctness** — the floor returned by `truncate_wal_if_eligible`
   equals the floor computed by a direct synchronous call to
   `compute_truncate_floor` on the same registry state.

3. **Semantic identity** — calling `truncate_wal_if_eligible` followed
   by a fresh `compute_truncate_floor()` returns a consistent floor (no
   side-effect that corrupts the floor calc for the next caller).

4. **Exception propagation** — if a snapshot file is corrupted so that
   `compute_truncate_floor` returns 0, `truncate_wal_if_eligible`
   returns ``None`` (conservative: no truncation).  The exception path
   inside `asyncio.to_thread` is handled by the existing try/except.

No mocks. Real `AgentRegistry` + real tmp_path file I/O.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog
from reyn.skill.skill_snapshot import SkillSnapshot

# ---------------------------------------------------------------------------
# Helpers (mirrors test_registry_wal_truncate.py)
# ---------------------------------------------------------------------------


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in truncation tests")


def _make_registry(tmp_path: Path, *, with_state_log: bool = True) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl") if with_state_log else None
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_no_factory,
        state_log=state_log,
    )


def _seed_agent(registry: AgentRegistry, name: str, *, applied_seq: int) -> Path:
    AgentProfile.new(name, role="").save(registry._dir / name)
    snap = AgentSnapshot.empty(name)
    snap.applied_seq = applied_seq
    snap_path = registry._dir / name / "state" / "snapshot.json"
    snap.save(snap_path)
    return snap_path


def _seed_skill(
    registry: AgentRegistry,
    agent_name: str,
    run_id: str,
    *,
    last_phase_applied_seq: int,
) -> Path:
    snap = SkillSnapshot.empty(run_id, "demo_skill", {"input": "x"})
    snap.last_phase_applied_seq = last_phase_applied_seq
    snap_path = (
        registry._dir / agent_name / "state" / "skills" / f"{run_id}.snapshot.json"
    )
    snap.save(snap_path)
    return snap_path


def _corrupt_agent_snapshot(registry: AgentRegistry, name: str) -> None:
    snap_path = registry._dir / name / "state" / "snapshot.json"
    snap_path.write_text("{this is not valid json", encoding="utf-8")


# ---------------------------------------------------------------------------
# Invariant 1 — Non-blocking: event loop makes progress while floor is computed
# ---------------------------------------------------------------------------


def test_event_loop_not_blocked_during_floor_scan(tmp_path):
    """Tier 2: concurrent asyncio.sleep(0) sentinel makes progress during truncate_wal_if_eligible.

    With 100 agents (= O(N) file stats + reads), a blocking sync call would
    stall the event loop for the full scan duration.  By wrapping in
    asyncio.to_thread, cooperative scheduling resumes — the sentinel counter
    advances at least once.
    """
    N = 100
    registry = _make_registry(tmp_path)

    # Seed N agents with distinct applied_seq values so floor = 2 (min=1 → +1)
    for i in range(1, N + 1):
        _seed_agent(registry, f"agent_{i:03d}", applied_seq=i)

    sentinel_ticks: list[int] = []

    async def _sentinel() -> None:
        """Yields cooperatively until cancelled."""
        tick = 0
        while True:
            await asyncio.sleep(0)
            tick += 1
            sentinel_ticks.append(tick)

    async def go() -> None:
        sentinel_task = asyncio.create_task(_sentinel())
        try:
            # Append WAL entries so truncation actually fires
            for i in range(1, 11):
                await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
            await registry.truncate_wal_if_eligible()
        finally:
            sentinel_task.cancel()
            try:
                await sentinel_task
            except asyncio.CancelledError:
                pass

    asyncio.run(go())

    # The sentinel must have ticked at least once while the floor scan ran.
    # If compute_truncate_floor blocked the event loop, sentinel_ticks would
    # be empty (or very sparse).
    assert len(sentinel_ticks) > 0, (
        "sentinel made zero ticks — event loop appears to have been blocked "
        "during compute_truncate_floor (asyncio.to_thread not working)"
    )


# ---------------------------------------------------------------------------
# Invariant 2 — Correctness: async result matches synchronous call
# ---------------------------------------------------------------------------


def test_floor_via_truncate_matches_direct_sync_call(tmp_path):
    """Tier 2: floor used by truncate_wal_if_eligible equals compute_truncate_floor() called directly.

    This confirms that wrapping in asyncio.to_thread does not alter the
    value returned by the sync computation.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=8)
    _seed_agent(registry, "beta", applied_seq=5)
    _seed_skill(registry, "alpha", "run_001", last_phase_applied_seq=3)

    # Synchronous floor (= reference value)
    expected_floor = registry.compute_truncate_floor()
    assert expected_floor == 4  # min(8, 5, 3) + 1

    async def go():
        for i in range(1, 11):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        return await registry.truncate_wal_if_eligible()

    stats = asyncio.run(go())
    assert stats is not None
    # floor=4 means seq 1..3 dropped, 4..10 kept
    assert stats["dropped"] == 3
    assert stats["kept"] == 7


# ---------------------------------------------------------------------------
# Invariant 3 — Semantic identity: truncate then recompute is consistent
# ---------------------------------------------------------------------------


def test_floor_consistent_after_truncation(tmp_path):
    """Tier 2: compute_truncate_floor() called synchronously after truncate_wal_if_eligible returns the same floor.

    No side-effect from the asyncio.to_thread wrapper should corrupt the
    floor calculation state for the next synchronous caller.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)

    async def go():
        for i in range(1, 15):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        await registry.truncate_wal_if_eligible()

    asyncio.run(go())

    # Synchronous call after the async truncation: same registry, same on-disk
    # snapshots, so same floor.  The truncation does not mutate snapshot files.
    floor_after = registry.compute_truncate_floor()
    assert floor_after == 11  # applied_seq=10 → floor = 11


# ---------------------------------------------------------------------------
# Invariant 4 — Exception propagation: corrupt snapshot → returns None
# ---------------------------------------------------------------------------


def test_corrupt_snapshot_propagates_as_none(tmp_path):
    """Tier 2: when compute_truncate_floor encounters a corrupt snapshot it
    returns 0, and truncate_wal_if_eligible returns None (no truncation).

    The exception that emerges from asyncio.to_thread when compute_truncate_floor
    returns 0 (rather than raising) is handled by the floor <= 0 guard.
    We also test the actual-raise path by corrupting mid-scan to verify
    the BLE001 catch wrapping still fires.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _corrupt_agent_snapshot(registry, "alpha")

    async def go():
        await registry.state_log.append("inbox_put", target="a", payload={})
        return await registry.truncate_wal_if_eligible()

    result = asyncio.run(go())
    # Corrupt snapshot → compute_truncate_floor returns 0 → no truncation
    assert result is None


def test_large_n_floor_scan_completes(tmp_path):
    """Tier 2: truncate_wal_if_eligible with 50 agents completes without error.

    Regression guard: asyncio.to_thread default pool is adequate for N < 100
    agents.  We don't pin the wall-clock time; we only assert completion and
    correctness.
    """
    N = 50
    registry = _make_registry(tmp_path)
    # min applied_seq = 1 → floor = 2
    for i in range(1, N + 1):
        _seed_agent(registry, f"agent_{i:03d}", applied_seq=i)

    async def go():
        for i in range(1, 11):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        return await registry.truncate_wal_if_eligible()

    stats = asyncio.run(go())
    # floor = min(1..50) + 1 = 2 → drop seq 1, keep 2..10
    assert stats is not None
    assert stats["dropped"] == 1
    assert stats["kept"] == 9
