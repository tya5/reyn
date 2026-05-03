"""Tier 2: OS invariant — SkillResumeAnalyzer correctly classifies WAL events into committed vs ambiguous steps.

The analyzer is the read-only foundation for forward-replay resume.
A misclassification here would cause the runtime to either:
  - skip a step that did NOT commit (lose work), or
  - re-execute a step that DID commit (duplicate side effects).

Both are unrecoverable in the transactional-replay pattern, so this layer is
heavily tested.

Observation flows through:
  - The ResumePlan dataclass returned by analyze()
  - No file I/O — fixtures provide synthetic SkillSnapshots and event
    lists directly
"""
from __future__ import annotations

import pytest

from reyn.skill.skill_resume_analyzer import (
    AmbiguousStep,
    CommittedStep,
    ResumePlan,
    SkillResumeAnalyzer,
)
from reyn.skill.skill_snapshot import SkillSnapshot


def _snap(
    *,
    run_id: str = "r1",
    current_phase: str = "draft",
    history: list[str] | None = None,
    visit_counts: dict[str, int] | None = None,
) -> SkillSnapshot:
    s = SkillSnapshot.empty(run_id, "demo", {"x": 1})
    s.current_phase = current_phase
    s.history = history or []
    s.visit_counts = visit_counts or {}
    return s


def _step_started(
    *, seq: int, oid: str, phase: str = "draft",
    op_kind: str = "file", args_hash: str = "abcd",
    args: dict | None = None,
) -> dict:
    return {
        "seq": seq,
        "kind": "step_started",
        "op_invocation_id": oid,
        "op_kind": op_kind,
        "phase": phase,
        "args_hash": args_hash,
        "args": args or {"op": "write"},
    }


def _step_completed(
    *, seq: int, oid: str, phase: str = "draft",
    op_kind: str = "file", args_hash: str = "abcd",
    result: object = None,
) -> dict:
    return {
        "seq": seq,
        "kind": "step_completed",
        "op_invocation_id": oid,
        "op_kind": op_kind,
        "phase": phase,
        "args_hash": args_hash,
        "result": result if result is not None else {"ok": True},
    }


def _step_failed(
    *, seq: int, oid: str, phase: str = "draft",
    op_kind: str = "file", args_hash: str = "abcd",
    error_kind: str = "exception", message: str = "boom",
) -> dict:
    return {
        "seq": seq,
        "kind": "step_failed",
        "op_invocation_id": oid,
        "op_kind": op_kind,
        "phase": phase,
        "args_hash": args_hash,
        "error_kind": error_kind,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Happy path: clean started/completed pairs
# ---------------------------------------------------------------------------


def test_paired_started_and_completed_become_committed_no_ambiguity():
    """Tier 2: a step_started followed by a matching step_completed is committed; no ambiguity."""
    analyzer = SkillResumeAnalyzer()
    snap = _snap()
    events = [
        _step_started(seq=10, oid="draft.0"),
        _step_completed(seq=11, oid="draft.0", result={"ok": True}),
    ]
    plan = analyzer.analyze(snapshot=snap, wal_events=events)
    assert len(plan.committed_steps) == 1
    assert len(plan.ambiguous_steps) == 0
    assert plan.has_ambiguity is False
    cs = plan.committed_steps[0]
    assert cs.op_invocation_id == "draft.0"
    assert cs.result == {"ok": True}
    assert cs.error_kind is None


def test_world_op_with_only_completed_event_is_committed():
    """Tier 2: world / llm purity ops emit only step_completed; analyzer treats it as a standalone committed step (no started to pair against)."""
    analyzer = SkillResumeAnalyzer()
    snap = _snap()
    events = [
        _step_completed(seq=10, oid="draft.0", op_kind="web_fetch",
                        result={"content": "hello"}),
    ]
    plan = analyzer.analyze(snapshot=snap, wal_events=events)
    assert len(plan.committed_steps) == 1
    assert plan.committed_steps[0].op_kind == "web_fetch"
    assert len(plan.ambiguous_steps) == 0


def test_failed_step_is_committed_with_error_kind():
    """Tier 2: step_failed paired with started → committed step bearing error_kind, not result."""
    analyzer = SkillResumeAnalyzer()
    snap = _snap()
    events = [
        _step_started(seq=10, oid="draft.0"),
        _step_failed(seq=11, oid="draft.0", error_kind="permission_denied",
                     message="nope"),
    ]
    plan = analyzer.analyze(snapshot=snap, wal_events=events)
    assert len(plan.committed_steps) == 1
    cs = plan.committed_steps[0]
    assert cs.error_kind == "permission_denied"
    assert cs.error_message == "nope"
    assert cs.result is None
    assert plan.has_ambiguity is False


# ---------------------------------------------------------------------------
# Ambiguous detection — the intermediate-state case
# ---------------------------------------------------------------------------


def test_orphan_started_without_completion_is_ambiguous():
    """Tier 2: a step_started with no matching completion → AmbiguousStep.

    This is THE case that requires operator decision (retry / skip /
    discard). The analyzer must surface it explicitly.
    """
    analyzer = SkillResumeAnalyzer()
    snap = _snap()
    events = [
        _step_started(seq=10, oid="draft.0", op_kind="mcp",
                      args={"tool": "create"}),
        # No completion — process crashed mid-op
    ]
    plan = analyzer.analyze(snapshot=snap, wal_events=events)
    assert plan.has_ambiguity is True
    assert len(plan.ambiguous_steps) == 1
    assert len(plan.committed_steps) == 0
    amb = plan.ambiguous_steps[0]
    assert amb.op_invocation_id == "draft.0"
    assert amb.op_kind == "mcp"
    assert amb.args == {"tool": "create"}
    assert amb.started_seq == 10


def test_completed_pairs_with_oldest_unpaired_started_for_repeated_invocation_ids():
    """Tier 2: when the same op_invocation_id appears twice (phase revisit), the completed event pairs with the *oldest* unpaired started.

    Pairing in WAL order disambiguates: each completion claims the
    earliest matching started.
    """
    analyzer = SkillResumeAnalyzer()
    snap = _snap()
    events = [
        _step_started(seq=10, oid="draft.0", args_hash="hash_v1"),
        _step_started(seq=12, oid="draft.0", args_hash="hash_v2"),  # phase revisit
        _step_completed(seq=15, oid="draft.0", args_hash="hash_v1",
                        result={"v": 1}),
        # second started has no completion → ambiguous
    ]
    plan = analyzer.analyze(snapshot=snap, wal_events=events)
    assert len(plan.committed_steps) == 1
    assert len(plan.ambiguous_steps) == 1
    # Committed pairs with the older started (args_hash hash_v1)
    assert plan.committed_steps[0].args_hash == "hash_v1"
    # Ambiguous is the newer started
    assert plan.ambiguous_steps[0].args_hash == "hash_v2"
    assert plan.ambiguous_steps[0].started_seq == 12


def test_multiple_ambiguous_steps_sorted_by_started_seq():
    """Tier 2: when several started events orphan, they appear in the plan ordered by started_seq (oldest first) for deterministic UX."""
    analyzer = SkillResumeAnalyzer()
    snap = _snap()
    events = [
        _step_started(seq=20, oid="draft.1"),
        _step_started(seq=10, oid="draft.0"),  # appended out of order
        _step_started(seq=30, oid="draft.2"),
    ]
    plan = analyzer.analyze(snapshot=snap, wal_events=events)
    seqs = [a.started_seq for a in plan.ambiguous_steps]
    assert seqs == [10, 20, 30]


# ---------------------------------------------------------------------------
# Snapshot fields propagate verbatim
# ---------------------------------------------------------------------------


def test_snapshot_fields_propagate_to_plan():
    """Tier 2: ResumePlan reflects all snapshot bookkeeping fields verbatim."""
    analyzer = SkillResumeAnalyzer()
    snap = _snap(
        run_id="r_xyz",
        current_phase="review",
        history=["draft", "draft", "review"],
        visit_counts={"draft": 2, "review": 1},
    )
    snap.last_phase_artifact_path = "ws/v3.json"
    snap.awaiting_intervention_id = "iv-a"

    plan = analyzer.analyze(snapshot=snap, wal_events=[])
    assert plan.run_id == "r_xyz"
    assert plan.skill_name == "demo"
    assert plan.skill_input == {"x": 1}
    assert plan.current_phase == "review"
    assert plan.last_phase_artifact_path == "ws/v3.json"
    assert plan.awaiting_intervention_id == "iv-a"
    assert plan.phases_visited == ["draft", "draft", "review"]
    assert plan.visit_counts == {"draft": 2, "review": 1}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_event_stream_yields_clean_plan():
    """Tier 2: zero WAL events → committed and ambiguous lists are empty."""
    analyzer = SkillResumeAnalyzer()
    plan = analyzer.analyze(snapshot=_snap(), wal_events=[])
    assert plan.committed_steps == []
    assert plan.ambiguous_steps == []
    assert plan.has_ambiguity is False


def test_unknown_event_kinds_are_ignored():
    """Tier 2: lifecycle events (skill_started, etc.) and unrelated kinds don't create false committed/ambiguous entries."""
    analyzer = SkillResumeAnalyzer()
    snap = _snap()
    events = [
        {"seq": 1, "kind": "skill_started", "run_id": "r1"},
        {"seq": 2, "kind": "skill_phase_advanced", "next_phase": "draft"},
        _step_started(seq=3, oid="draft.0"),
        _step_completed(seq=4, oid="draft.0"),
        {"seq": 5, "kind": "intervention_dispatched"},
        {"seq": 6, "kind": "skill_completed"},
    ]
    plan = analyzer.analyze(snapshot=snap, wal_events=events)
    assert len(plan.committed_steps) == 1
    assert len(plan.ambiguous_steps) == 0


def test_completion_without_matching_started_still_recorded():
    """Tier 2: a completed event whose started is missing (e.g. earlier WAL was truncated) is still recorded as committed.

    Truncation cannot drop entries below the active skill's
    last_phase_applied_seq, but conservatively the analyzer must
    handle "started not seen" gracefully — record the completion as a
    standalone committed step (matches the world/llm purity case).
    """
    analyzer = SkillResumeAnalyzer()
    snap = _snap()
    events = [
        _step_completed(seq=10, oid="draft.0", result={"v": "old"}),
    ]
    plan = analyzer.analyze(snapshot=snap, wal_events=events)
    assert len(plan.committed_steps) == 1
    assert plan.committed_steps[0].result == {"v": "old"}
    assert plan.ambiguous_steps == []
