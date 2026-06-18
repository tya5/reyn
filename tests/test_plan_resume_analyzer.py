"""Tier 2: PlanResumeAnalyzer (ADR-0023 §3.2 Phase 2 step 7a).

Pairs WAL plan_step_* events + PlanSnapshot into a PlanResumePlan with
4-state per-step union: pending / completed_with_result / failed /
interrupted_with_child.
"""
from __future__ import annotations

from reyn.core.plan import (
    PlanResumeAnalyzer,
    PlanResumePlan,
    PlanSnapshot,
)
from reyn.runtime.planner import Plan, PlanStep


def _decomp() -> Plan:
    return Plan(
        goal="g",
        steps=(
            PlanStep("s1", "first", ()),
            PlanStep("s2", "second", (), depends_on=("s1",)),
            PlanStep("s3", "third", (), depends_on=("s2",)),
        ),
    )


def _empty_snap() -> PlanSnapshot:
    return PlanSnapshot.empty(
        plan_id="p001", agent_name="default", chain_id="c0", goal="g",
    )


# ── 4-state pairing ──────────────────────────────────────────────────────


def test_no_events_all_pending() -> None:
    """Tier 2: a plan with no step events yields all-pending state_states."""
    analyzer = PlanResumeAnalyzer()
    rp = analyzer.analyze(
        snapshot=_empty_snap(), decomposition=_decomp(), wal_events=[],
    )
    assert rp.n_steps == 3
    assert all(s.state == "pending" for s in rp.step_states)
    assert rp.pending_step_ids == ("s1", "s2", "s3")
    assert rp.has_ambiguity is False


def test_started_completed_pair_yields_completed_state() -> None:
    """Tier 2: started+completed pair → completed_with_result; result_text
    pulled from snapshot.step_results."""
    snap = _empty_snap()
    snap.step_results["s1"] = "step one output"
    events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
        {"seq": 2, "kind": "plan_step_completed", "plan_id": "p001",
         "step_id": "s1", "content_len": 16},
    ]
    rp = PlanResumeAnalyzer().analyze(
        snapshot=snap, decomposition=_decomp(), wal_events=events,
    )
    s1 = next(s for s in rp.step_states if s.step_id == "s1")
    assert s1.state == "completed_with_result"
    assert s1.result_text == "step one output"
    assert rp.committed_step_ids == frozenset({"s1"})
    assert rp.step_result_map() == {"s1": "step one output"}


def test_started_failed_pair_yields_failed_state() -> None:
    """Tier 2: started+failed pair → failed with error_message."""
    events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
        {"seq": 2, "kind": "plan_step_failed", "plan_id": "p001",
         "step_id": "s1", "error": "RuntimeError('boom')"},
    ]
    rp = PlanResumeAnalyzer().analyze(
        snapshot=_empty_snap(), decomposition=_decomp(), wal_events=events,
    )
    s1 = next(s for s in rp.step_states if s.step_id == "s1")
    assert s1.state == "failed"
    assert "boom" in (s1.error_message or "")
    assert rp.failed_step_ids == ("s1",)


def test_started_no_terminal_non_effectful_yields_pending() -> None:
    """Tier 2: started without terminal + non-effectful tools → pending
    (= safe to re-execute)."""
    events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
    ]
    rp = PlanResumeAnalyzer().analyze(
        snapshot=_empty_snap(), decomposition=_decomp(), wal_events=events,
    )
    s1 = next(s for s in rp.step_states if s.step_id == "s1")
    assert s1.state == "pending"
    assert s1.is_effectful is False


def test_started_no_terminal_effectful_yields_failed_ambiguous() -> None:
    """Tier 2: started without terminal + effectful tools → failed with
    error_kind="ambiguous_no_terminal" (= prevents silent double-write)."""
    decomp = Plan(
        goal="g",
        steps=(PlanStep("s1", "writer", tools=("write_file",)),),
    )
    events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
    ]
    snap = _empty_snap()
    rp = PlanResumeAnalyzer().analyze(
        snapshot=snap, decomposition=decomp, wal_events=events,
    )
    s1 = rp.step_states[0]
    assert s1.state == "failed"
    assert s1.error_kind == "ambiguous_no_terminal"
    assert s1.is_effectful is True
    assert rp.has_ambiguity is True


def test_started_no_terminal_with_child_yields_interrupted_with_child() -> None:
    """Tier 2: invoke_skill step started + spawned child snapshot recorded
    → interrupted_with_child state."""
    decomp = Plan(
        goal="g",
        steps=(PlanStep("s1", "delegate", tools=("invoke_skill",)),),
    )
    events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
    ]
    snap = _empty_snap()
    snap.spawned_skill_run_ids["s1"] = "child_xyz"

    looked_up = {"child_xyz": "in_flight"}
    rp = PlanResumeAnalyzer().analyze(
        snapshot=snap, decomposition=decomp, wal_events=events,
        child_skill_lookup=lambda rid: looked_up.get(rid, "unknown"),
    )
    s1 = rp.step_states[0]
    assert s1.state == "interrupted_with_child"
    assert s1.child_run_id == "child_xyz"
    assert s1.child_state == "in_flight"
    assert rp.has_in_flight_child is True
    assert rp.has_ambiguity is True
    assert rp.interrupted_with_child_step_ids == ("s1",)


def test_child_state_unknown_when_lookup_omitted() -> None:
    """Tier 2: when child_skill_lookup is None, child_state defaults to
    'unknown' so coordinator can default-cancel."""
    decomp = Plan(
        goal="g",
        steps=(PlanStep("s1", "delegate", tools=("invoke_skill",)),),
    )
    events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
    ]
    snap = _empty_snap()
    snap.spawned_skill_run_ids["s1"] = "child_xyz"
    rp = PlanResumeAnalyzer().analyze(
        snapshot=snap, decomposition=decomp, wal_events=events,
    )
    assert rp.step_states[0].child_state == "unknown"


# ── filtering ────────────────────────────────────────────────────────────


def test_analyzer_filters_other_plan_ids() -> None:
    """Tier 2: events for a different plan_id are ignored."""
    events = [
        # different plan_id — should be ignored
        {"seq": 1, "kind": "plan_step_started", "plan_id": "OTHER", "step_id": "s1"},
        {"seq": 2, "kind": "plan_step_completed", "plan_id": "OTHER",
         "step_id": "s1", "content_len": 1},
        # this plan
        {"seq": 3, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
        {"seq": 4, "kind": "plan_step_completed", "plan_id": "p001",
         "step_id": "s1", "content_len": 1},
    ]
    snap = _empty_snap()
    snap.step_results["s1"] = "ok"
    rp = PlanResumeAnalyzer().analyze(
        snapshot=snap, decomposition=_decomp(), wal_events=events,
    )
    assert rp.step_states[0].state == "completed_with_result"


def test_analyzer_carries_decomposition_artifact_path() -> None:
    """Tier 2: PlanResumePlan exposes the snapshot's
    decomposition_artifact_path so the runtime can reload from SSoT."""
    snap = _empty_snap()
    snap.decomposition_artifact_path = "/abs/path/decomposition.json"
    rp = PlanResumeAnalyzer().analyze(
        snapshot=snap, decomposition=_decomp(), wal_events=[],
    )
    assert rp.decomposition_artifact_path == "/abs/path/decomposition.json"


# ── multi-step combination ───────────────────────────────────────────────


def test_mixed_states_across_three_steps() -> None:
    """Tier 2: realistic crash mid-step-3 — s1 completed, s2 failed, s3
    pending (= no events for it yet)."""
    snap = _empty_snap()
    snap.step_results["s1"] = "first output"
    events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
        {"seq": 2, "kind": "plan_step_completed", "plan_id": "p001",
         "step_id": "s1", "content_len": 12},
        {"seq": 3, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s2"},
        {"seq": 4, "kind": "plan_step_failed", "plan_id": "p001",
         "step_id": "s2", "error": "boom"},
    ]
    rp = PlanResumeAnalyzer().analyze(
        snapshot=snap, decomposition=_decomp(), wal_events=events,
    )
    states = {s.step_id: s.state for s in rp.step_states}
    assert states == {
        "s1": "completed_with_result",
        "s2": "failed",
        "s3": "pending",
    }
