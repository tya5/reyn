"""Tier 2: PlanRegistry lifecycle + snapshot mutations (ADR-0023 §3.1).

Step 3 of the Phase 2 migration path. Mirrors test_skill_registry.py
shape — start, complete, step mutations, load_active.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.planner import Plan, PlanStep
from reyn.core.plan import (
    PlanRegistry,
    PlanSnapshot,
    decomposition_dir,
    plan_snapshot_path,
    write_decomposition,
)


def _make_registry(tmp_path: Path) -> PlanRegistry:
    return PlanRegistry(agent_name="default", agent_state_dir=tmp_path)


def _sample_plan() -> Plan:
    return Plan(
        goal="g",
        steps=(
            PlanStep("s1", "first", ()),
            PlanStep("s2", "second", (), depends_on=()),
        ),
    )


# ── start ─────────────────────────────────────────────────────────────────


def test_start_creates_snapshot_file(tmp_path: Path) -> None:
    """Tier 2: start saves the snapshot at the documented path."""
    reg = _make_registry(tmp_path)
    snap = reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    assert snap.plan_id == "p001"
    assert plan_snapshot_path(tmp_path, "p001").exists()
    assert reg.get("p001") is snap


def test_start_stamps_applied_seq_and_truncation_watermark(tmp_path: Path) -> None:
    """Tier 2: start initializes both applied_seq and last_step_applied_seq
    to the WAL seq the caller received from plan_started (= ADR-0023 §3.1
    initial stamp behaviour)."""
    reg = _make_registry(tmp_path)
    snap = reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=42)
    assert snap.applied_seq == 42
    assert snap.last_step_applied_seq == 42


def test_start_records_decomposition_and_steps(tmp_path: Path) -> None:
    """Tier 2: start carries decomposition_artifact_path + steps_serialized
    (= snapshot-side fallback when artifact unreadable)."""
    reg = _make_registry(tmp_path)
    serialized = [{"id": "s1", "description": "d", "tools": [], "depends_on": []}]
    snap = reg.start(
        plan_id="p001",
        chain_id="c1",
        goal="g",
        applied_seq=1,
        decomposition_artifact_path="/abs/path/decomposition.json",
        steps_serialized=serialized,
    )
    assert snap.decomposition_artifact_path == "/abs/path/decomposition.json"
    assert snap.steps_serialized == serialized


def test_start_idempotent_overwrites_in_memory_cache(tmp_path: Path) -> None:
    """Tier 2: starting the same plan_id twice replaces the in-memory entry
    (= mirrors SkillRegistry.start defensive overwrite)."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g1", applied_seq=1)
    snap2 = reg.start(plan_id="p001", chain_id="c2", goal="g2", applied_seq=10)
    assert reg.get("p001") is snap2
    assert reg.get("p001").goal == "g2"


# ── step mutations ────────────────────────────────────────────────────────


def test_record_step_started_sets_current_step_no_truncation_bump(tmp_path: Path) -> None:
    """Tier 2: step_started updates current_step_id and applied_seq, but
    does NOT bump last_step_applied_seq (= mirror SkillRegistry's
    step_started non-bump per ADR-0023 §3.1)."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    reg.record_step_started(plan_id="p001", step_id="s1", applied_seq=15)
    snap = reg.get("p001")
    assert snap.current_step_id == "s1"
    assert snap.applied_seq == 15
    assert snap.last_step_applied_seq == 10  # unchanged


@pytest.mark.asyncio
async def test_record_step_completed_bumps_truncation_watermark(tmp_path: Path) -> None:
    """Tier 2: step_completed bumps both applied_seq and last_step_applied_seq
    (= durable progress, gates WAL truncation)."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    reg.record_step_started(plan_id="p001", step_id="s1", applied_seq=15)
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text="hello world",
    )
    snap = reg.get("p001")
    assert snap.applied_seq == 20
    assert snap.last_step_applied_seq == 20
    assert snap.step_results == {"s1": "hello world"}
    assert snap.last_committed_step_id == "s1"
    assert snap.current_step_id is None  # cleared


@pytest.mark.asyncio
async def test_record_step_completed_spills_long_result_to_file(tmp_path: Path) -> None:
    """Tier 2: ADR-0024 — step results > 32 KB spill to a per-plan
    workspace file (= state/plans/<plan_id>/step_results/<step_id>.txt)
    and snapshot.step_results does NOT contain the entry; instead
    snapshot.step_result_refs holds the relative path. Full text is
    preserved (no [truncated] suffix)."""
    from reyn.core.plan import get_step_result, step_result_file_path

    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    huge = "x" * 100_000
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text=huge,
    )
    snap = reg.get("p001")
    # Inline dict does NOT contain s1 (= spilled, not inline).
    assert "s1" not in snap.step_results
    # Ref dict has the relative path.
    assert snap.step_result_refs["s1"] == "step_results/s1.txt"
    # File on disk contains the full text (= no truncation).
    spilled_path = step_result_file_path(tmp_path, "p001", "s1")
    assert spilled_path.exists()
    assert spilled_path.read_text(encoding="utf-8") == huge
    # Read accessor returns the full text transparently.
    assert get_step_result(snap, tmp_path, "s1") == huge


@pytest.mark.asyncio
async def test_record_step_completed_inline_for_small_result(tmp_path: Path) -> None:
    """Tier 2: ADR-0024 — small step results stay inline on
    snapshot.step_results without touching the file system (= cheap
    read path for chat-like steps)."""
    from reyn.core.plan import step_result_file_path

    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    small = "short reply"
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text=small,
    )
    snap = reg.get("p001")
    assert snap.step_results["s1"] == small
    assert "s1" not in snap.step_result_refs
    # No file written for small results.
    assert not step_result_file_path(tmp_path, "p001", "s1").exists()


@pytest.mark.asyncio
async def test_record_step_completed_re_run_clears_stale_inline(tmp_path: Path) -> None:
    """Tier 2: re-running a step that previously was small with a now-
    large output spills to file AND removes the stale inline entry."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=15, result_text="small",
    )
    huge = "x" * 50_000
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text=huge,
    )
    snap = reg.get("p001")
    assert "s1" not in snap.step_results          # stale inline cleared
    assert snap.step_result_refs["s1"] == "step_results/s1.txt"


@pytest.mark.asyncio
async def test_record_step_completed_re_run_clears_stale_spill(tmp_path: Path) -> None:
    """Tier 2: re-running a previously spilled step with a small result
    moves to inline AND deletes the stale spilled file."""
    from reyn.core.plan import step_result_file_path

    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    huge = "x" * 50_000
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=15, result_text=huge,
    )
    spilled_path = step_result_file_path(tmp_path, "p001", "s1")
    assert spilled_path.exists()

    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text="small",
    )
    snap = reg.get("p001")
    assert snap.step_results["s1"] == "small"
    assert "s1" not in snap.step_result_refs
    # Stale spilled file deleted.
    assert not spilled_path.exists()


@pytest.mark.asyncio
async def test_record_step_failed_records_error_and_bumps_watermark(tmp_path: Path) -> None:
    """Tier 2: step_failed bumps last_step_applied_seq (conservative — a
    recorded failure is real progress, prevents stale-WAL replay)."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    reg.record_step_started(plan_id="p001", step_id="s1", applied_seq=15)
    await reg.record_step_failed(
        plan_id="p001", step_id="s1", applied_seq=20,
        error_repr="RuntimeError('boom')",
    )
    snap = reg.get("p001")
    assert snap.last_step_applied_seq == 20
    assert snap.step_failures == {"s1": "RuntimeError('boom')"}
    assert snap.current_step_id is None


def test_record_child_spawned_tracks_step_to_child_mapping(tmp_path: Path) -> None:
    """Tier 2: spawned_skill_run_ids maps step_id → child_run_id, used by
    the resume coordinator for adopt/cancel decisions."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    reg.record_child_spawned(plan_id="p001", step_id="s1", child_run_id="child_xyz")
    snap = reg.get("p001")
    assert snap.spawned_skill_run_ids == {"s1": "child_xyz"}


def test_record_methods_no_op_for_unknown_plan_id(tmp_path: Path) -> None:
    """Tier 2: mutation methods are defensive — unknown plan_id logs and
    returns without raising (= safe under WAL replay edge cases)."""
    reg = _make_registry(tmp_path)
    # No start() call; record_* should be no-op.
    reg.record_step_started(plan_id="nonexistent", step_id="s1", applied_seq=1)
    reg.record_child_spawned(plan_id="nonexistent", step_id="s1", child_run_id="x")
    assert reg.list_active() == []


# ── complete ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_removes_snapshot_file_and_artifact(tmp_path: Path) -> None:
    """Tier 2: complete deletes both the snapshot file AND the
    decomposition artifact (= P5 cleanup per ADR-0023 §3.4 finally)."""
    reg = _make_registry(tmp_path)
    write_decomposition(tmp_path, "p001", _sample_plan())
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)

    await reg.complete(plan_id="p001")

    assert not plan_snapshot_path(tmp_path, "p001").exists()
    assert not (decomposition_dir(tmp_path, "p001") / "decomposition.json").exists()
    assert reg.get("p001") is None
    assert reg.list_active() == []


@pytest.mark.asyncio
async def test_complete_preserves_artifact_when_requested(tmp_path: Path) -> None:
    """Tier 2: delete_artifact=False preserves the decomposition for
    forensics."""
    reg = _make_registry(tmp_path)
    write_decomposition(tmp_path, "p001", _sample_plan())
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)

    await reg.complete(plan_id="p001", delete_artifact=False)

    assert not plan_snapshot_path(tmp_path, "p001").exists()
    assert (decomposition_dir(tmp_path, "p001") / "decomposition.json").exists()


@pytest.mark.asyncio
async def test_complete_idempotent_on_unknown_plan(tmp_path: Path) -> None:
    """Tier 2: complete on an unknown plan_id is safe (= AgentRegistry
    cleanup may call this on snapshots that were already deleted)."""
    reg = _make_registry(tmp_path)
    await reg.complete(plan_id="nonexistent")  # no raise
    await reg.complete(plan_id="nonexistent", status="aborted")  # no raise


# ── load_active ───────────────────────────────────────────────────────────


def test_load_active_discovers_existing_snapshots(tmp_path: Path) -> None:
    """Tier 2: load_active populates the in-memory cache from disk
    (= startup recovery before any new activity)."""
    reg1 = _make_registry(tmp_path)
    reg1.start(plan_id="p001", chain_id="c1", goal="g1", applied_seq=10)
    reg1.start(plan_id="p002", chain_id="c2", goal="g2", applied_seq=20)

    # Fresh registry — no in-memory state until load_active.
    reg2 = _make_registry(tmp_path)
    assert reg2.list_active() == []
    loaded = reg2.load_active()
    assert set(loaded.keys()) == {"p001", "p002"}
    assert reg2.get("p001").goal == "g1"
    assert reg2.get("p002").goal == "g2"


def test_load_active_skips_corrupt_files(tmp_path: Path) -> None:
    """Tier 2: corrupt snapshot files are skipped with a warning, not
    crashing load_active (= WAL is source of truth, replay rebuilds)."""
    reg1 = _make_registry(tmp_path)
    reg1.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)

    # Corrupt a sibling file.
    corrupt_path = plan_snapshot_path(tmp_path, "corrupt")
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("xxx not a snapshot xxx", encoding="utf-8")

    reg2 = _make_registry(tmp_path)
    loaded = reg2.load_active()
    # Corrupt file loads as empty (mirror SkillSnapshot.load resilience),
    # so it's still in the cache but with default fields. The key
    # invariant is: no crash + valid plans still load.
    assert "p001" in loaded
    assert loaded["p001"].goal == "g"


# ── truncate hook ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_truncate_hook_fires_on_step_completed_and_complete(tmp_path: Path) -> None:
    """Tier 2: truncate_eligible_hook fires after durable mutations
    (step_completed, step_failed, complete) — mirrors SkillRegistry."""
    fired: list[str] = []

    async def hook() -> None:
        fired.append("hit")

    reg = PlanRegistry(
        agent_name="default", agent_state_dir=tmp_path,
        truncate_eligible_hook=hook,
    )
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    # start does NOT fire hook (= no durable progress); record_step_started doesn't either.
    reg.record_step_started(plan_id="p001", step_id="s1", applied_seq=15)
    assert fired == []

    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text="x",
    )
    assert fired == ["hit"]

    await reg.record_step_failed(
        plan_id="p001", step_id="s2", applied_seq=25,
        error_repr="boom",
    )
    assert fired == ["hit", "hit"]

    await reg.complete(plan_id="p001")
    assert fired == ["hit", "hit", "hit"]


# ── reset_from_step (= /plan resume escape hatch) ──────────────────────


@pytest.mark.asyncio
async def test_reset_from_step_clears_results_from_target_onward(tmp_path: Path) -> None:
    """Tier 2: reset_from_step clears step_results / step_failures /
    spawned_skill_run_ids for the target step and every step after it
    in topological order. Steps before are preserved (= memo replay
    on next launch)."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=15, result_text="r1",
    )
    await reg.record_step_completed(
        plan_id="p001", step_id="s2", applied_seq=20, result_text="r2",
    )
    await reg.record_step_completed(
        plan_id="p001", step_id="s3", applied_seq=25, result_text="r3",
    )
    reg.record_child_spawned(
        plan_id="p001", step_id="s2", child_run_id="child_s2",
    )

    ok = reg.reset_from_step(
        plan_id="p001", from_step_id="s2",
        step_order=["s1", "s2", "s3"],
    )
    assert ok is True

    snap = reg.get("p001")
    # s1 preserved
    assert snap.step_results.get("s1") == "r1"
    # s2, s3 cleared
    assert "s2" not in snap.step_results
    assert "s3" not in snap.step_results
    # spawned_skill_run_ids cleared for s2 too
    assert "s2" not in snap.spawned_skill_run_ids
    # last_committed_step_id rewinds to s1 (= step before from_step_id)
    assert snap.last_committed_step_id == "s1"
    assert snap.current_step_id is None


def test_reset_from_step_first_step_clears_everything(tmp_path: Path) -> None:
    """Tier 2: from_step_id == head step → all results cleared,
    last_committed_step_id resets to None."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    snap = reg.get("p001")
    snap.step_results["s1"] = "r1"
    snap.step_results["s2"] = "r2"
    snap.last_committed_step_id = "s2"

    ok = reg.reset_from_step(
        plan_id="p001", from_step_id="s1", step_order=["s1", "s2"],
    )
    assert ok is True
    snap = reg.get("p001")
    assert snap.step_results == {}
    assert snap.last_committed_step_id is None


def test_reset_from_step_unknown_plan_returns_false(tmp_path: Path) -> None:
    """Tier 2: unknown plan_id → False, no mutation."""
    reg = _make_registry(tmp_path)
    ok = reg.reset_from_step(
        plan_id="nonexistent", from_step_id="s1", step_order=["s1"],
    )
    assert ok is False


def test_reset_from_step_unknown_step_returns_false(tmp_path: Path) -> None:
    """Tier 2: from_step_id not in step_order → False, no mutation."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    snap = reg.get("p001")
    snap.step_results["s1"] = "r1"

    ok = reg.reset_from_step(
        plan_id="p001", from_step_id="nonexistent",
        step_order=["s1", "s2"],
    )
    assert ok is False
    # Snapshot unchanged
    assert reg.get("p001").step_results == {"s1": "r1"}


@pytest.mark.asyncio
async def test_reset_from_step_persists_to_disk(tmp_path: Path) -> None:
    """Tier 2: mutated snapshot is persisted before return so a crash
    mid-reset doesn't leave inconsistent state."""
    reg = _make_registry(tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=15, result_text="r1",
    )
    await reg.record_step_completed(
        plan_id="p001", step_id="s2", applied_seq=20, result_text="r2",
    )

    reg.reset_from_step(
        plan_id="p001", from_step_id="s2", step_order=["s1", "s2"],
    )

    # Reload from disk into a fresh registry — confirms persistence.
    reg2 = _make_registry(tmp_path)
    reg2.load_active()
    snap2 = reg2.get("p001")
    assert snap2.step_results == {"s1": "r1"}


@pytest.mark.asyncio
async def test_truncate_hook_exceptions_swallowed(tmp_path: Path) -> None:
    """Tier 2: hook raising an exception doesn't break the registry
    (= truncation is opportunistic)."""
    async def bad_hook() -> None:
        raise RuntimeError("boom")

    reg = PlanRegistry(
        agent_name="default", agent_state_dir=tmp_path,
        truncate_eligible_hook=bad_hook,
    )
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    # Should not raise.
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text="x",
    )
    await reg.complete(plan_id="p001")
