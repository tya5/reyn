"""Tier 2: OS invariant — act-turn runtime-only rewind (#1533 2a-3, ADR-0038 D6).

Act-turn granularity is reachable (not durable) via `snapshot(step-start) +
CommittedStep memo` 0-token Ghost-Replay. Rewinding a skill run to an act-turn
boundary K = truncating the resume memo at K: keep only `committed_steps` with
`seq <= K` (they Ghost-Replay) and drop later ones (they re-execute). Pure reuse
of `SkillResumeAnalyzer` + the existing dispatch memo — runtime-only by
construction (touches only the resume plan / skill-run state, never the workspace).

Real `SkillResumeAnalyzer` (no mocks); synthetic SkillSnapshot + WAL event dicts,
mirroring tests/test_skill_resume_analyzer.py.
"""
from __future__ import annotations

from reyn.skill.skill_resume_coordinator import SkillResumeCoordinator
from reyn.skill.skill_snapshot import SkillSnapshot


def _snap(run_id: str = "r1", phase: str = "draft") -> SkillSnapshot:
    s = SkillSnapshot.empty(run_id, "demo", {"x": 1})
    s.current_phase = phase
    return s


def _completed(*, seq: int, oid: str, phase: str = "draft") -> dict:
    return {
        "seq": seq, "kind": "step_completed", "op_invocation_id": oid,
        "op_kind": "file", "phase": phase, "args_hash": f"h{oid}",
        "result": {"ok": True, "oid": oid},
    }


def _started(*, seq: int, oid: str, phase: str = "draft") -> dict:
    return {
        "seq": seq, "kind": "step_started", "op_invocation_id": oid,
        "op_kind": "file", "phase": phase, "args_hash": f"h{oid}",
        "args": {"op": "write"},
    }


def _committed_seqs(plan) -> list[int]:
    return sorted(c.seq for c in plan.committed_steps)


# ── memo truncation at the act-turn boundary ──────────────────────────────────


def test_rewind_truncates_committed_steps_at_target_seq():
    """Tier 2: rewind to K keeps committed steps with seq <= K, drops the rest.

    Three world-purity steps committed at seqs 2, 4, 6 (each its own committed
    step — no started pairing). Rewind to K=4 → the memo retains {2, 4}; step 6
    falls out, so on relaunch it re-executes (the act-turn rewind). Steps 2,4
    remain memoized (0-token Ghost-Replay).
    """
    coord = SkillResumeCoordinator()
    snap = _snap()
    events = [
        _completed(seq=2, oid="a"),
        _completed(seq=4, oid="b"),
        _completed(seq=6, oid="c"),
    ]

    plan = coord.plan_for_act_turn_rewind(
        snapshot=snap, wal_events=events, target_seq=4,
    )

    assert _committed_seqs(plan) == [2, 4]      # 6 dropped → re-executes
    # the surviving memo carries the recorded results (Ghost-Replay payload intact).
    by_oid = {c.op_invocation_id: c for c in plan.committed_steps}
    assert by_oid["a"].result == {"ok": True, "oid": "a"}
    assert "c" not in by_oid
    # identity preserved (run/phase) — same run, just a shorter memo.
    assert plan.run_id == "r1" and plan.current_phase == "draft"


def test_rewind_target_at_or_after_last_step_is_full_memo():
    """Tier 2: target >= the last committed seq keeps the whole memo (no-op rewind)."""
    coord = SkillResumeCoordinator()
    events = [_completed(seq=2, oid="a"), _completed(seq=5, oid="b")]

    plan = coord.plan_for_act_turn_rewind(
        snapshot=_snap(), wal_events=events, target_seq=99,
    )
    assert _committed_seqs(plan) == [2, 5]


def test_rewind_target_before_first_step_empties_memo():
    """Tier 2: target below the first committed seq → empty memo (full re-exec from phase start)."""
    coord = SkillResumeCoordinator()
    events = [_completed(seq=4, oid="a"), _completed(seq=6, oid="b")]

    plan = coord.plan_for_act_turn_rewind(
        snapshot=_snap(), wal_events=events, target_seq=1,
    )
    assert plan.committed_steps == []


def test_rewind_bounds_ambiguous_steps_by_target_seq():
    """Tier 2: an ambiguous (unpaired started) after K is dropped from the rewound plan.

    A started@3 never completes (ambiguous). Rewinding to K=2 must drop it — a
    step started after the rewind point is not part of the rewound-to state. A
    started@1 (also unpaired, <= K) is retained for the operator decision.
    """
    coord = SkillResumeCoordinator()
    events = [
        _started(seq=1, oid="early"),    # ambiguous, <= K → kept
        _completed(seq=2, oid="done"),   # committed, <= K → kept
        _started(seq=3, oid="late"),     # ambiguous, > K → dropped
    ]

    plan = coord.plan_for_act_turn_rewind(
        snapshot=_snap(), wal_events=events, target_seq=2,
    )

    assert _committed_seqs(plan) == [2]
    amb_seqs = sorted(a.started_seq for a in plan.ambiguous_steps)
    assert amb_seqs == [1]                       # late@3 dropped
