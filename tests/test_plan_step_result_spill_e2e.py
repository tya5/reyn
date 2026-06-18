"""Tier 2: ADR-0024 spill round-trip — large step results survive crash.

Pins the headline correctness invariant of ADR-0024: a step whose
output exceeds the 32 KB inline threshold writes to a per-plan
workspace file, persists across a simulated crash + restart, and is
restored verbatim by the resume analyzer (= no truncation, no data
loss).

Tier 2 + standalone — no real LLM, no async event loop integration.
Exercises:
  - PlanRegistry.record_step_completed spill branch
  - PlanSnapshot save/load round-trip with step_result_refs
  - PlanResumeAnalyzer reads via get_step_result accessor
  - PlanResumeCoordinator forwards agent_state_dir
  - PlanRegistry.complete cleanup via delete_plan_workspace
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.plan import (
    PlanRegistry,
    PlanResumeAnalyzer,
    PlanResumeCoordinator,
    PlanSnapshot,
    decomposition_dir,
    delete_plan_workspace,
    get_step_result,
    plan_snapshot_path,
    step_result_file_path,
    write_decomposition,
)
from reyn.runtime.planner import Plan, PlanStep


def _decomp() -> Plan:
    return Plan(
        goal="g",
        steps=(
            PlanStep("s1", "first", ()),
            PlanStep("s2", "second", (), depends_on=("s1",)),
        ),
    )


# ── round-trip ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_50kb_step_result_round_trip(tmp_path: Path) -> None:
    """Tier 2: 50 KB step output → spill → reload PlanSnapshot from
    disk → analyzer reconstructs full text via get_step_result. No
    [truncated] suffix anywhere."""
    plan = _decomp()
    write_decomposition(tmp_path, "p001", plan)

    # Phase A: register + complete s1 with large text.
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    huge_text = "X" * 50_000  # well above 32 KB threshold
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text=huge_text,
    )

    # Spill landed; full text on disk.
    spilled_path = step_result_file_path(tmp_path, "p001", "s1")
    assert spilled_path.exists()
    assert len(spilled_path.read_text(encoding="utf-8")) == 50_000

    # Phase B: simulate crash → restart by reloading from disk.
    reg2 = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg2.load_active()
    snap2 = reg2.get("p001")
    assert snap2 is not None
    # In-memory snapshot has the ref but NOT the inline text.
    assert "s1" not in snap2.step_results
    assert snap2.step_result_refs["s1"] == "step_results/s1.txt"

    # Phase C: analyzer rebuilds resume_plan via get_step_result.
    analyzer = PlanResumeAnalyzer()
    synthetic_events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001",
         "step_id": "s1"},
        {"seq": 2, "kind": "plan_step_completed", "plan_id": "p001",
         "step_id": "s1", "content_len": len(huge_text)},
    ]
    rp = analyzer.analyze(
        snapshot=snap2, decomposition=plan, wal_events=synthetic_events,
        agent_state_dir=tmp_path,
    )
    s1_state = next(s for s in rp.step_states if s.step_id == "s1")
    assert s1_state.state == "completed_with_result"
    # Full 50KB recovered — NOT truncated.
    assert s1_state.result_text == huge_text
    assert "[truncated]" not in (s1_state.result_text or "")


@pytest.mark.asyncio
async def test_get_step_result_returns_full_text_after_reload(tmp_path: Path) -> None:
    """Tier 2: get_step_result accessor reads the spilled file
    transparently regardless of whether the snapshot is freshly built
    in memory or freshly loaded from disk."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    text = "Y" * 80_000
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text=text,
    )

    # Same-process: read via accessor.
    snap1 = reg.get("p001")
    assert get_step_result(snap1, tmp_path, "s1") == text

    # Reload-from-disk: read via accessor.
    snap2 = PlanSnapshot.load(
        "p001", plan_snapshot_path(tmp_path, "p001"),
    )
    assert get_step_result(snap2, tmp_path, "s1") == text


# ── corruption fallback (ADR-0024 §4) ────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_spill_file_classifies_as_failed(tmp_path: Path) -> None:
    """Tier 2: ADR-0024 §4 — when step_result_refs[sid] is set but the
    file is missing/unreadable, the analyzer marks the step as
    failed("step_result_file_missing") so the coordinator's discard
    policy applies (= safe failure)."""
    plan = _decomp()
    write_decomposition(tmp_path, "p001", plan)

    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    text = "Z" * 50_000
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=20, result_text=text,
    )

    # Simulate file corruption: delete the spilled file out from under us.
    step_result_file_path(tmp_path, "p001", "s1").unlink()

    # Reload + analyze.
    reg2 = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg2.load_active()
    snap2 = reg2.get("p001")
    analyzer = PlanResumeAnalyzer()
    synthetic_events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001",
         "step_id": "s1"},
        {"seq": 2, "kind": "plan_step_completed", "plan_id": "p001",
         "step_id": "s1", "content_len": 50_000},
    ]
    rp = analyzer.analyze(
        snapshot=snap2, decomposition=plan, wal_events=synthetic_events,
        agent_state_dir=tmp_path,
    )
    s1_state = next(s for s in rp.step_states if s.step_id == "s1")
    assert s1_state.state == "failed"
    assert s1_state.error_kind == "step_result_file_missing"


# ── workspace cleanup on plan completion (ADR-0024 §3.3) ─────────────────


@pytest.mark.asyncio
async def test_complete_recursively_removes_per_plan_workspace(tmp_path: Path) -> None:
    """Tier 2: PlanRegistry.complete uses delete_plan_workspace which
    rmtree's the per-plan dir — including spilled step files. The legacy
    delete_decomposition path would orphan the dir because step_results/
    isn't empty."""
    write_decomposition(tmp_path, "p001", _decomp())
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=15,
        result_text="A" * 50_000,
    )
    await reg.record_step_completed(
        plan_id="p001", step_id="s2", applied_seq=20,
        result_text="B" * 50_000,
    )

    plan_dir = decomposition_dir(tmp_path, "p001")
    assert plan_dir.is_dir()
    assert (plan_dir / "step_results" / "s1.txt").exists()
    assert (plan_dir / "step_results" / "s2.txt").exists()

    await reg.complete(plan_id="p001")

    # Whole per-plan dir reclaimed.
    assert not plan_dir.exists()


# ── coordinator integration (ADR-0024 thread-through) ───────────────────


@pytest.mark.asyncio
async def test_coordinator_forwards_agent_state_dir_to_analyzer(tmp_path: Path) -> None:
    """Tier 2: PlanResumeCoordinator.discover_and_decide threads
    agent_state_dir through to PlanResumeAnalyzer.analyze so spilled
    refs resolve to full text in the resulting decision."""
    plan = _decomp()
    write_decomposition(tmp_path, "p001", plan)

    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    huge = "Q" * 60_000
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=15, result_text=huge,
    )

    def loader(plan_id: str) -> Plan:
        return plan

    coord = PlanResumeCoordinator()
    decisions = coord.discover_and_decide(
        plan_registry=reg,
        wal_events=[
            {"seq": 1, "kind": "plan_step_started", "plan_id": "p001",
             "step_id": "s1"},
            {"seq": 2, "kind": "plan_step_completed", "plan_id": "p001",
             "step_id": "s1", "content_len": 60_000},
        ],
        decomposition_loader=loader,
        agent_state_dir=tmp_path,
    )
    s1_state = next(
        s for s in decisions[0].plan.step_states if s.step_id == "s1"
    )
    assert s1_state.state == "completed_with_result"
    assert s1_state.result_text == huge   # full 60 KB preserved


# ── reset_from_step deletes spilled files (ADR-0024 §3.4) ────────────────


@pytest.mark.asyncio
async def test_reset_from_step_deletes_spilled_files(tmp_path: Path) -> None:
    """Tier 2: reset_from_step clears step_result_refs entries AND
    deletes the underlying spilled files so re-execution starts with a
    clean disk state."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    await reg.record_step_completed(
        plan_id="p001", step_id="s1", applied_seq=15,
        result_text="A" * 50_000,
    )
    await reg.record_step_completed(
        plan_id="p001", step_id="s2", applied_seq=20,
        result_text="B" * 50_000,
    )
    s2_path = step_result_file_path(tmp_path, "p001", "s2")
    assert s2_path.exists()

    reg.reset_from_step(
        plan_id="p001", from_step_id="s2", step_order=["s1", "s2"],
    )

    snap = reg.get("p001")
    assert "s2" not in snap.step_result_refs
    # s1 spilled file untouched
    assert step_result_file_path(tmp_path, "p001", "s1").exists()
    # s2 spilled file deleted
    assert not s2_path.exists()
