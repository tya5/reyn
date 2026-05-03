"""Tier 2: OS invariant — AgentRegistry's WAL truncation orchestrator.

The orchestrator gathers `applied_seq` across every agent + every active
skill snapshot, computes the universally-absorbed floor, and rewrites
the WAL to drop entries below it. This is the policy layer on top of
`StateLog.truncate_below` (the rewrite primitive, tested separately).

Tests target the public surface (`truncate_wal_if_eligible`,
`_compute_truncate_floor`); observation flows through:
  - the on-disk WAL (re-read after truncation)
  - the returned stats dict
No mocks — we construct real `AgentRegistry` instances backed by real
snapshots on a temporary `tmp_path`.

Policy compliance:
- No `unittest.mock` usage (Fake > Mock per docs/ja/contributing/testing.md)
- No private-state assertion beyond `_last_truncation_ts` for throttle
  observation, which has no public surface and is the cleanest probe
  (alternative: time-travel patches, which would be worse)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog
from reyn.skill.skill_snapshot import SkillSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_factory(_profile):
    """Session factory used by tests that never instantiate a session."""
    raise AssertionError("session factory must not be called in truncation tests")


def _make_registry(tmp_path: Path, *, with_state_log: bool = True) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl") if with_state_log else None
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_no_factory,
        state_log=state_log,
    )


def _seed_agent(registry: AgentRegistry, name: str, *, applied_seq: int) -> Path:
    """Create the on-disk profile + snapshot for an agent with the given applied_seq."""
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
    """Create the on-disk per-skill snapshot for an agent's active skill."""
    snap = SkillSnapshot.empty(run_id, "demo_skill", {"input": "x"})
    snap.last_phase_applied_seq = last_phase_applied_seq
    snap_path = (
        registry._dir / agent_name / "state" / "skills" / f"{run_id}.snapshot.json"
    )
    snap.save(snap_path)
    return snap_path


def _wal_seqs(state_log: StateLog) -> set[int]:
    return {e["seq"] for e in state_log.iter_from(0) if isinstance(e.get("seq"), int)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_compute_floor_uses_min_applied_seq_across_agents(tmp_path):
    """Tier 2: floor = min(全 agent applied_seq) + 1 when no active skills exist."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_agent(registry, "beta", applied_seq=4)
    _seed_agent(registry, "gamma", applied_seq=7)

    floor = registry._compute_truncate_floor()
    assert floor == 5  # min(10, 4, 7) + 1


def test_compute_floor_includes_active_skill_snapshots(tmp_path):
    """Tier 2: an in-flight skill at last_phase_applied_seq=2 forces floor=3 even if all agents are at 10."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_agent(registry, "beta", applied_seq=10)
    _seed_skill(registry, "alpha", "run_xyz", last_phase_applied_seq=2)

    floor = registry._compute_truncate_floor()
    assert floor == 3  # min(10, 10, 2) + 1


def test_compute_floor_zero_when_no_persistent_agents(tmp_path):
    """Tier 2: dormant agents (snapshot-less) are skipped; with zero persistent agents, floor=0 (no truncation).

    The registry __init__ auto-creates `default` without a snapshot file
    — it's truly dormant (no session ever instantiated this run, no events
    can target it). Excluding it from the floor calc, no agents constrain
    the WAL and floor=0 (treat as 'don't know, don't truncate').
    """
    registry = _make_registry(tmp_path)
    # `default` was auto-created but has no snapshot.json — dormant, skipped.
    floor = registry._compute_truncate_floor()
    assert floor == 0


def test_compute_floor_zero_on_corrupt_agent_snapshot(tmp_path):
    """Tier 2: corrupt agent snapshot returns floor=0 (fail closed — keep WAL intact).

    Truncation parses agent snapshots explicitly (rather than going through
    ``AgentSnapshot.load``'s defensive empty-fallback) so corruption is
    distinguishable from the legitimate dormant case (``applied_seq == 0``).
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=5)
    snap_path = registry._dir / "alpha" / "state" / "snapshot.json"
    snap_path.write_text("{not valid json", encoding="utf-8")

    floor = registry._compute_truncate_floor()
    assert floor == 0


def test_compute_floor_skips_dormant_agent_with_zero_applied_seq(tmp_path):
    """Tier 2: a snapshot-on-disk with applied_seq=0 (e.g. written by restore_all for an unused agent) is skipped from floor calc.

    Semantically equivalent to "snapshot file missing" — the agent has
    never absorbed a WAL event, so it doesn't constrain truncation. This
    invariant matters because PR21 ``restore_all`` writes baseline
    snapshots for every known agent at restart time.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    _seed_agent(registry, "dormant", applied_seq=0)  # restore_all-style baseline

    floor = registry._compute_truncate_floor()
    # Dormant skipped → floor based on alpha alone
    assert floor == 11


def test_compute_floor_zero_on_corrupt_skill_snapshot(tmp_path):
    """Tier 2: malformed skill snapshot is conservative — returns 0, no truncation."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    skills_dir = registry._dir / "alpha" / "state" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "bad.snapshot.json").write_text("{garbage", encoding="utf-8")

    floor = registry._compute_truncate_floor()
    assert floor == 0


def test_truncate_eligible_drops_below_floor_and_returns_stats(tmp_path):
    """Tier 2: truncate_wal_if_eligible drops entries with seq below the computed floor."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=8)

    async def go():
        # Append 10 WAL entries
        for i in range(1, 11):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        return await registry.truncate_wal_if_eligible()

    stats = asyncio.run(go())
    assert stats is not None
    # alpha applied_seq=8 → floor = 9; drop seq 1..8, keep 9, 10
    assert stats["dropped"] == 8
    assert stats["kept"] == 2
    assert _wal_seqs(registry.state_log) == {9, 10}


def test_truncate_throttled_within_window(tmp_path):
    """Tier 2: a second call within `_TRUNCATION_THROTTLE_SECS` returns None without rewriting."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=8)

    async def go():
        for i in range(1, 11):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        first = await registry.truncate_wal_if_eligible()
        # Append more, then immediately re-attempt
        await registry.state_log.append("inbox_put", target="extra", payload={})
        second = await registry.truncate_wal_if_eligible()
        return first, second

    first, second = asyncio.run(go())
    assert first is not None
    assert second is None  # throttled


def test_truncate_no_op_when_no_state_log(tmp_path):
    """Tier 2: registry with state_log=None returns None — defensive for tests / non-chat invocation."""
    registry = _make_registry(tmp_path, with_state_log=False)
    _seed_agent(registry, "alpha", applied_seq=5)

    async def go():
        return await registry.truncate_wal_if_eligible()

    assert asyncio.run(go()) is None


def test_truncate_no_op_when_floor_not_advanced(tmp_path):
    """Tier 2: floor=0 (corrupt skill snapshot) skips the rewrite entirely."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    skills_dir = registry._dir / "alpha" / "state" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "bad.snapshot.json").write_text("{garbage", encoding="utf-8")

    async def go():
        await registry.state_log.append("inbox_put", target="a", payload={})
        return await registry.truncate_wal_if_eligible()

    result = asyncio.run(go())
    assert result is None
    # WAL untouched
    assert _wal_seqs(registry.state_log) == {1}


def test_truncate_advances_seqs_across_active_skill_completion(tmp_path):
    """Tier 2: end-to-end progression — active skill phase-advances, snapshot updates, next truncation drops the older range."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=10)
    skill_snap_path = _seed_skill(
        registry, "alpha", "run_zzz", last_phase_applied_seq=3,
    )

    async def go():
        for i in range(1, 21):
            await registry.state_log.append("inbox_put", target=f"a{i}", payload={})
        # First truncation: floor = min(10, 3) + 1 = 4 → drop 1..3
        first = await registry.truncate_wal_if_eligible()
        # Skill phase-advances to seq 15 — bump its snapshot
        snap = SkillSnapshot.load("run_zzz", skill_snap_path)
        snap.last_phase_applied_seq = 15
        snap.save(skill_snap_path)
        # Wait past throttle window
        registry._last_truncation_ts = None
        # Second truncation: floor = min(10, 15) + 1 = 11 → drop 4..10
        second = await registry.truncate_wal_if_eligible()
        return first, second

    first, second = asyncio.run(go())
    assert first["dropped"] == 3
    assert first["kept"] == 17
    assert second["dropped"] == 7
    # Surviving: 11..20
    assert _wal_seqs(registry.state_log) == set(range(11, 21))
