"""Tier 2: R-D8 L1+L2 — CommittedStep carries optional usage data.

For LLM steps, the WAL ``step_completed`` event must include the
``usage`` (TokenUsage data) so the resume path can re-credit the budget
tracker via ``record_llm`` on memo hit. Without this, post-crash budget
shows lower than actual and caps are bypassed.

Backward compat: existing WAL events without ``usage`` field load
cleanly with ``CommittedStep.usage = None``; memo hit logs a warning
and skips the credit (graceful degradation).
"""
from __future__ import annotations

import asyncio

from reyn.events.state_log import StateLog
from reyn.skill.skill_resume_analyzer import (
    CommittedStep,
    SkillResumeAnalyzer,
)
from reyn.skill.skill_snapshot import SkillSnapshot


def _snap(run_id: str = "run_x") -> SkillSnapshot:
    return SkillSnapshot(
        skill_run_id=run_id,
        skill_name="demo",
        skill_input={"type": "input", "data": {}},
        applied_seq=0,
        last_phase_applied_seq=0,
        current_phase="draft",
        history=["draft"],
        visit_counts={"draft": 1},
    )


# ---------------------------------------------------------------------------
# L1: CommittedStep accepts usage
# ---------------------------------------------------------------------------


def test_committed_step_default_usage_is_none():
    """Tier 2: CommittedStep created without usage → usage is None (backward compat)."""
    cs = CommittedStep(
        op_invocation_id="draft.0",
        op_kind="file",
        phase="draft",
        args_hash="abc",
        seq=10,
        result={"path": "x.txt"},
    )
    assert cs.usage is None


def test_committed_step_accepts_usage():
    """Tier 2: CommittedStep accepts a usage dict."""
    cs = CommittedStep(
        op_invocation_id="draft.llm.0",
        op_kind="llm",
        phase="draft",
        args_hash="abc",
        seq=10,
        result={"type": "finish"},
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    )
    assert cs.usage == {
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
    }


# ---------------------------------------------------------------------------
# L1: Analyzer reads usage from WAL step_completed
# ---------------------------------------------------------------------------


def test_analyzer_extracts_usage_from_step_completed_for_llm(tmp_path):
    """Tier 2: WAL ``step_completed`` with usage → CommittedStep.usage populated."""
    sl = StateLog(tmp_path / "wal.jsonl")

    async def write():
        await sl.append(
            "skill_started",
            target="alpha", agent="alpha",
            run_id="run_x", skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        await sl.append(
            "step_completed",
            target="alpha", agent="alpha",
            run_id="run_x", phase="draft",
            op_invocation_id="draft.llm.0",
            op_kind="llm", args_hash="abc123",
            result={"type": "finish"},
            usage={"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
        )

    asyncio.run(write())

    analyzer = SkillResumeAnalyzer()
    plan = analyzer.analyze(
        snapshot=_snap("run_x"),
        wal_events=list(sl.iter_from(0)),
    )
    llm_committed = [s for s in plan.committed_steps if s.op_kind == "llm"]
    assert len(llm_committed) == 1
    assert llm_committed[0].usage == {
        "prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280,
    }


def test_analyzer_handles_missing_usage_field_backward_compat(tmp_path):
    """Tier 2: pre-R-D8 WAL events (no usage field) → CommittedStep.usage is None."""
    sl = StateLog(tmp_path / "wal.jsonl")

    async def write():
        await sl.append(
            "skill_started",
            target="alpha", agent="alpha",
            run_id="run_old",
            skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        await sl.append(
            "step_completed",
            target="alpha", agent="alpha",
            run_id="run_old", phase="draft",
            op_invocation_id="draft.llm.0",
            op_kind="llm", args_hash="abc123",
            result={"type": "finish"},
            # NO usage field
        )

    asyncio.run(write())

    analyzer = SkillResumeAnalyzer()
    plan = analyzer.analyze(
        snapshot=_snap("run_old"),
        wal_events=list(sl.iter_from(0)),
    )
    llm_committed = [s for s in plan.committed_steps if s.op_kind == "llm"]
    assert len(llm_committed) == 1
    assert llm_committed[0].usage is None


def test_analyzer_extracts_usage_for_non_llm_steps_too(tmp_path):
    """Tier 2: usage field is op_kind-agnostic.

    Op-kind steps don't normally carry usage but the analyzer should
    pass it through if present rather than discriminate by op_kind.
    Future use cases (e.g. shell wall-clock cost recording) get free
    plumbing.
    """
    sl = StateLog(tmp_path / "wal.jsonl")

    async def write():
        await sl.append(
            "skill_started",
            target="alpha", agent="alpha",
            run_id="run_op",
            skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        await sl.append(
            "step_completed",
            target="alpha", agent="alpha",
            run_id="run_op", phase="draft",
            op_invocation_id="draft.0",
            op_kind="file", args_hash="abc",
            result={"path": "x.txt"},
            usage={"bytes_written": 4096},
        )

    asyncio.run(write())

    analyzer = SkillResumeAnalyzer()
    plan = analyzer.analyze(
        snapshot=_snap("run_op"),
        wal_events=list(sl.iter_from(0)),
    )
    file_committed = [s for s in plan.committed_steps if s.op_kind == "file"]
    assert len(file_committed) == 1
    assert file_committed[0].usage == {"bytes_written": 4096}


# ---------------------------------------------------------------------------
# L2: WAL event accepts usage parameter
# ---------------------------------------------------------------------------


def test_state_log_accepts_usage_in_step_completed(tmp_path):
    """Tier 2: ``state_log.append('step_completed', ..., usage=...)`` round-trips.

    The WAL frame stores usage as an arbitrary dict. Validation only at
    read time (analyzer) — write side is permissive.
    """
    sl = StateLog(tmp_path / "wal.jsonl")

    async def go():
        await sl.append(
            "step_completed",
            run_id="run_y", phase="draft",
            op_invocation_id="draft.llm.0",
            op_kind="llm", args_hash="x",
            result={},
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )

    asyncio.run(go())
    events = list(sl.iter_from(0))
    completed = [e for e in events if e["kind"] == "step_completed"]
    assert len(completed) == 1
    assert completed[0]["usage"] == {
        "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
    }
