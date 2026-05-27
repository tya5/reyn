"""Tier 2: OS invariant — long-await skill exclusion from WAL truncation floor (R-D16).

R-D4 established the WAL size safety net but a single skill stuck on
``ask_user`` (e.g. user away from terminal) would pin the truncation floor
at its ``last_phase_applied_seq`` indefinitely, causing unbounded WAL growth
in multi-agent / long-session deployments.

R-D16 fixes this: skills with ``awaiting_since`` older than
``_LONG_AWAIT_THRESHOLD_SEC`` (= 300s) are excluded from the floor calc.
The trade-off is memo loss for the awaited window — at resume the awaited
op falls through to re-execute, identical to a memo cache miss.

These tests target the public surface:
  - ``SkillSnapshot`` save/load (the persisted field)
  - ``AgentRegistry.compute_truncate_floor`` (called via the public
    ``truncate_wal_if_eligible``; same probe pattern as
    ``test_registry_wal_truncate.py``)

No mocks; real instances. ``time.monotonic`` is monkeypatched inside the
registry module to make the elapsed-time decision deterministic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog
from reyn.skill.skill_snapshot import SkillSnapshot

# ---------------------------------------------------------------------------
# Helpers (mirror test_registry_wal_truncate.py for consistency)
# ---------------------------------------------------------------------------


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in floor tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_no_factory,
        state_log=state_log,
    )


def _seed_agent(registry: AgentRegistry, name: str, *, applied_seq: int) -> None:
    AgentProfile.new(name, role="").save(registry._dir / name)
    snap = AgentSnapshot.empty(name)
    snap.applied_seq = applied_seq
    snap.save(registry._dir / name / "state" / "snapshot.json")


def _seed_skill(
    registry: AgentRegistry,
    agent_name: str,
    run_id: str,
    *,
    last_phase_applied_seq: int,
    awaiting_since: float | None = None,
) -> Path:
    snap = SkillSnapshot.empty(run_id, "demo_skill", {"input": "x"})
    snap.last_phase_applied_seq = last_phase_applied_seq
    snap.awaiting_since = awaiting_since
    snap_path = (
        registry._dir / agent_name / "state" / "skills" / f"{run_id}.snapshot.json"
    )
    snap.save(snap_path)
    return snap_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_awaiting_since_round_trip(tmp_path: Path):
    """Tier 2: SkillSnapshot.save / load preserves ``awaiting_since``."""
    snap = SkillSnapshot.empty("run-rt", "sk", {})
    snap.awaiting_since = 999.5
    path = tmp_path / "skills" / "run-rt.snapshot.json"
    snap.save(path)
    loaded = SkillSnapshot.load("run-rt", path)
    assert loaded.awaiting_since == 999.5


def test_floor_includes_short_await_skill(tmp_path: Path, monkeypatch):
    """Tier 2: a skill awaiting < 5 min still pins the floor (R-D4 behaviour)."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=1000)
    # awaiting_since "just now"; elapsed < threshold → floor must include it.
    _seed_skill(
        registry, "alpha", "run-short",
        last_phase_applied_seq=50, awaiting_since=100.0,
    )

    # monotonic at "now=200.0" → elapsed=100s, well under 300s threshold.
    monkeypatch.setattr("reyn.chat.registry.time.monotonic", lambda: 200.0)

    floor = registry.compute_truncate_floor()
    # min(agent=1000, skill=50) + 1 = 51
    assert floor == 51


def test_floor_excludes_long_await_skill(tmp_path: Path, monkeypatch):
    """Tier 2: a skill awaiting >= 5 min is removed from the floor (R-D16)."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=1000)
    _seed_skill(
        registry, "alpha", "run-long",
        last_phase_applied_seq=50, awaiting_since=100.0,
    )

    # now=500 → elapsed=400s > 300s threshold → skill excluded.
    monkeypatch.setattr("reyn.chat.registry.time.monotonic", lambda: 500.0)

    floor = registry.compute_truncate_floor()
    # Only the agent's applied_seq=1000 survives; floor = 1001 (not 51).
    assert floor == 1001


def test_floor_mixes_short_long_and_non_await(tmp_path: Path, monkeypatch):
    """Tier 2: mixed skills — only long-await is excluded; short-await
    and non-await both stay in the floor calc."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=1000)
    # short-await: elapsed 100s — included, last_phase=80
    _seed_skill(
        registry, "alpha", "run-short",
        last_phase_applied_seq=80, awaiting_since=400.0,
    )
    # long-await: elapsed 450s — excluded, last_phase=20
    _seed_skill(
        registry, "alpha", "run-long",
        last_phase_applied_seq=20, awaiting_since=50.0,
    )
    # non-await: awaiting_since=None — included, last_phase=70
    _seed_skill(
        registry, "alpha", "run-active",
        last_phase_applied_seq=70, awaiting_since=None,
    )

    monkeypatch.setattr("reyn.chat.registry.time.monotonic", lambda: 500.0)

    floor = registry.compute_truncate_floor()
    # min(agent=1000, short=80, active=70) + 1 = 71. Long is excluded.
    assert floor == 71


def test_clearing_awaiting_since_restores_inclusion(tmp_path: Path, monkeypatch):
    """Tier 2: when a long-await is resolved (awaiting_since=None), the
    skill is again included in the floor calc."""
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=1000)
    skill_path = _seed_skill(
        registry, "alpha", "run-x",
        last_phase_applied_seq=50, awaiting_since=10.0,
    )

    # First: long-await → excluded, floor=1001
    monkeypatch.setattr("reyn.chat.registry.time.monotonic", lambda: 500.0)
    assert registry.compute_truncate_floor() == 1001

    # User answered: clear awaiting_since on disk.
    snap = SkillSnapshot.load("run-x", skill_path)
    snap.awaiting_since = None
    snap.save(skill_path)

    # Now the skill is included again → floor pinned at 51.
    assert registry.compute_truncate_floor() == 51


def test_threshold_is_300_seconds(tmp_path: Path, monkeypatch):
    """Tier 2: the long-await threshold is exactly 300s — boundary check.

    Pinning the threshold value as a public-surface contract is fine
    because it's an OS-level constant (``_LONG_AWAIT_THRESHOLD_SEC``)
    documented in the registry module. This test guards the constant
    from accidentally being reduced or removed.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=1000)
    _seed_skill(
        registry, "alpha", "run-edge",
        last_phase_applied_seq=50, awaiting_since=100.0,
    )

    # Just under threshold (elapsed=299.9s) → still included → floor=51.
    monkeypatch.setattr("reyn.chat.registry.time.monotonic", lambda: 399.9)
    assert registry.compute_truncate_floor() == 51

    # At/over threshold (elapsed=300.0s) → excluded → floor=1001.
    monkeypatch.setattr("reyn.chat.registry.time.monotonic", lambda: 400.0)
    assert registry.compute_truncate_floor() == 1001


def test_long_await_alone_returns_zero(tmp_path: Path, monkeypatch):
    """Tier 2: when the only skill is long-awaiting and there are no
    other constrainers, floor falls back to 0 (= "don't truncate") —
    matching the existing "no constraints anywhere" branch.

    A dormant agent (applied_seq=0) is skipped per existing logic, so
    excluding the long-await skill leaves no seqs to consider.
    """
    registry = _make_registry(tmp_path)
    # Dormant agent (applied_seq=0) — skipped by existing floor logic
    _seed_agent(registry, "alpha", applied_seq=0)
    _seed_skill(
        registry, "alpha", "run-only",
        last_phase_applied_seq=50, awaiting_since=10.0,
    )

    monkeypatch.setattr("reyn.chat.registry.time.monotonic", lambda: 500.0)
    # All sources excluded → seqs empty → floor=0 (no truncation).
    assert registry.compute_truncate_floor() == 0


def test_unset_awaiting_since_treats_as_not_awaiting(tmp_path: Path, monkeypatch):
    """Tier 2: a snapshot saved without ``awaiting_since`` (None) is treated
    as not awaiting, regardless of how long ago the field was last written.
    This is the backward-compatible default for snapshots persisted before
    R-D16 landed.
    """
    registry = _make_registry(tmp_path)
    _seed_agent(registry, "alpha", applied_seq=1000)
    _seed_skill(
        registry, "alpha", "run-old",
        last_phase_applied_seq=50, awaiting_since=None,
    )

    # Even at t=10**9 monotonic, awaiting_since=None means "not awaiting"
    # → skill stays in the floor.
    monkeypatch.setattr("reyn.chat.registry.time.monotonic", lambda: 1_000_000_000.0)
    assert registry.compute_truncate_floor() == 51
