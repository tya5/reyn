"""Tier 2: OS invariant — AgentRegistry.list_rewind_points (ADR-0038 1f).

The time-travel UI (Stage 1f) enumerates rewind targets via this method. Each
row is one snapshot-generation boundary on the active branch:
``{"seq", "ts", "kind"}``. ``kind`` (turn / plan-step / phase) is derived from
the WAL entry kind at that seq — an OS-level execution boundary (P7-safe). The
audit EventStore is intentionally NOT consulted (WAL and audit stay decoupled).

Pins:
- boundary seqs come from the generation store (not every WAL seq);
- ts + kind are read from the WAL entry at each boundary seq;
- WAL kind → label mapping (skill_phase_advanced→phase, step_*→plan-step,
  else→turn);
- abandoned (rewound-past) boundaries are filtered out (is_active_seq);
- rows are ascending by seq.

Real AgentRegistry + StateLog + on-disk generations — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import rewind
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry, _rewind_point_kind


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _record_gen(reg: AgentRegistry, name: str, seq: int) -> None:
    """Persist a generation for ``name`` cut at boundary ``seq``."""
    snap = AgentSnapshot.empty(name)
    snap.applied_seq = seq
    reg._store_for(name).record(snap)


# ── unit: kind mapping ────────────────────────────────────────────────────────


def test_rewind_point_kind_mapping() -> None:
    """Tier 2: WAL entry kind → boundary label (turn / plan-step / phase)."""
    assert _rewind_point_kind("skill_phase_advanced") == "phase"
    assert _rewind_point_kind("step_completed") == "plan-step"
    assert _rewind_point_kind("step_failed") == "plan-step"
    assert _rewind_point_kind("plan_step_completed") == "plan-step"
    assert _rewind_point_kind("plan_step_failed") == "plan-step"
    assert _rewind_point_kind("inbox_consume") == "turn"
    assert _rewind_point_kind("") == "turn"


# ── integration: enumeration ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_rewind_points_reads_ts_and_kind_from_wal(tmp_path) -> None:
    """Tier 2: each boundary seq → {seq, ts, kind} read from the WAL entry.

    Generations cut at seqs 1 (inbox_consume→turn), 2 (step_completed→plan-step),
    3 (skill_phase_advanced→phase). The returned rows carry the WAL ts + the
    derived kind, ascending by seq.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log

    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    s2 = await log.append("step_completed", run_id="r1", step="s")
    s3 = await log.append("skill_phase_advanced", run_id="r1", phase="p")
    for s in (s1, s2, s3):
        _record_gen(reg, "alpha", s)

    rows = reg.list_rewind_points()

    assert [r["seq"] for r in rows] == [s1, s2, s3]  # ascending
    assert [r["kind"] for r in rows] == ["turn", "plan-step", "phase"]
    # ts is the WAL entry's timestamp — non-empty ISO string for each.
    assert all(r["ts"] for r in rows)


@pytest.mark.asyncio
async def test_list_rewind_points_only_generation_boundaries(tmp_path) -> None:
    """Tier 2: only seqs with a recorded generation appear (not every WAL seq)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log

    await log.append("inbox_consume", target="alpha", msg_id="m1")  # seq 1, NO gen
    s2 = await log.append("inbox_consume", target="alpha", msg_id="m2")  # seq 2, gen
    _record_gen(reg, "alpha", s2)

    rows = reg.list_rewind_points()
    assert [r["seq"] for r in rows] == [s2]


@pytest.mark.asyncio
async def test_list_rewind_points_filters_abandoned(tmp_path) -> None:
    """Tier 2: boundaries on an abandoned branch are excluded (is_active_seq)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log

    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    s2 = await log.append("inbox_consume", target="alpha", msg_id="m2")
    for s in (s1, s2):
        _record_gen(reg, "alpha", s)
    await rewind(log, target_n=s1)  # abandons (s1, R) — s2 is now inactive

    rows = reg.list_rewind_points()
    seqs = [r["seq"] for r in rows]
    assert s1 in seqs
    assert s2 not in seqs  # abandoned boundary filtered out


def test_list_rewind_points_empty_without_wal(tmp_path) -> None:
    """Tier 2: no WAL → empty list (no crash)."""
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    assert reg.list_rewind_points() == []
