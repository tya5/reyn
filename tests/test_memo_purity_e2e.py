"""Tier 3 (e2e): world purity skip — flaky read API recovers via resume.

Headline scenario the PR-memo-purity-fix is designed for. Without the
M2 skip, a flaky world op result memoized at run 1 would be replayed
forever on resume, locking the skill into a wrong path. With the
skip, resume re-executes the world op and gets a fresh result.

The test exercises three real components glued together:
  - dispatch_tool (memo lookup, purity classification)
  - StateLog WAL (durable step events)
  - SkillResumeAnalyzer (reads WAL → builds ResumePlan with
    committed_steps for the dispatcher to consult on the next run)

No LLM is involved (web_fetch is dispatched directly with a stub
invoker simulating the flaky-then-fresh API behavior).

Reference: PR-memo-purity-fix M4 in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.events.state_log import StateLog
from reyn.skill.skill_resume_analyzer import SkillResumeAnalyzer
from reyn.skill.skill_snapshot import SkillSnapshot

_RUN_ID = "run_e2e_purity"
_PHASE = "search"


class _FakeEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))


_CATALOG = {"web_fetch": {"function": {"name": "web_fetch"}}}


def _ctx(
    *,
    state_log: StateLog,
    resume_plan: Any | None = None,
) -> DispatchContext:
    return DispatchContext(
        caller_kind="skill_phase",
        caller_id="search_skill.search",
        chain_id="c_e2e",
        tool_catalog=_CATALOG,
        events=_FakeEvents(),
        state_log=state_log,
        skill_run_id=_RUN_ID,
        phase=_PHASE,
        resume_plan=resume_plan,
    )


def test_e2e_flaky_world_op_recovers_on_resume(tmp_path: Path):
    """Tier 3: flaky web_fetch → crash → resume → re-execute returns fresh.

    Flow:
      1. Run 1 — web_fetch invoker returns []. step_completed lands in
         WAL with the empty result.
      2. Skill snapshot saved (simulating advance_phase before crash);
         skill never completes (= active state survives).
      3. Run 2 — Analyzer rebuilds ResumePlan from WAL. dispatch_tool
         is invoked again with the same op_invocation_id + args; world
         purity bypasses memo, invoker returns ["fresh"].
      4. Verify: the fresh result is returned, NOT the recorded [].
      5. Verify: WAL has two step_completed events for this op (first
         empty, second fresh).
    """
    log_path = tmp_path / "wal.jsonl"
    snap_dir = tmp_path / "skills"
    snap_dir.mkdir()

    args = {"url": "https://flaky.example.com/search?q=foo"}
    op_invocation_id = "search.0"

    # ── Run 1: flaky ─────────────────────────────────────────────────
    log1 = StateLog(log_path)
    flaky_invoker_calls = []

    async def flaky_invoker(_args):
        flaky_invoker_calls.append(_args)
        return {"results": []}

    asyncio.run(dispatch_tool(
        name="web_fetch", args=args,
        ctx=_ctx(state_log=log1),
        invoker=flaky_invoker,
        op_invocation_id=op_invocation_id,
    ))
    assert flaky_invoker_calls == [args]

    # Save a snapshot simulating "phase still in progress when crash hit"
    # (= per-skill snapshot persists, skill_completed never fired).
    snap = SkillSnapshot(
        skill_run_id=_RUN_ID,
        skill_name="search_skill",
        skill_input={"type": "input", "data": {}},
        applied_seq=10,
        last_phase_applied_seq=10,
        current_phase=_PHASE,
        last_phase_artifact_path=None,
        history=[_PHASE],
        visit_counts={_PHASE: 1},
    )
    snap_path = snap_dir / f"{_RUN_ID}.snapshot.json"
    snap.save(snap_path)

    # WAL invariants from run 1
    run1_kinds = [e["kind"] for e in log1.iter_from(0)]
    assert run1_kinds.count("step_completed") == 1
    run1_completed = [e for e in log1.iter_from(0) if e["kind"] == "step_completed"][0]
    assert run1_completed["result"] == {"results": []}

    # ── Run 2: resume — rebuild plan, re-dispatch ────────────────────
    log2 = StateLog(log_path)
    snap_loaded = SkillSnapshot.load(_RUN_ID, snap_path)
    # Filter WAL events by run_id (matches SkillResumeAnalyzer contract).
    run_events = [
        e for e in log2.iter_from(0) if e.get("run_id") == _RUN_ID
    ]
    plan = SkillResumeAnalyzer().analyze(
        snapshot=snap_loaded,
        wal_events=run_events,
    )

    # Sanity: the prior run's flaky step is in committed_steps
    assert any(
        s.op_invocation_id == op_invocation_id and s.phase == _PHASE
        for s in plan.committed_steps
    ), f"Analyzer should surface prior step; got {plan.committed_steps}"

    fresh_invoker_calls = []

    async def fresh_invoker(_args):
        fresh_invoker_calls.append(_args)
        return {"results": ["fresh", "data"]}

    result = asyncio.run(dispatch_tool(
        name="web_fetch", args=args,
        ctx=_ctx(state_log=log2, resume_plan=plan),
        invoker=fresh_invoker,
        op_invocation_id=op_invocation_id,
    ))

    # Critical: world op skipped memo, fresh invoker was called
    assert fresh_invoker_calls == [args], (
        "world op must re-execute on resume despite committed_steps having a memo"
    )
    assert result == {"status": "ok", "data": {"results": ["fresh", "data"]}}

    # Run 2 also wrote a fresh step_completed (= future resume sees
    # the latest world view).
    all_completed = [e for e in log2.iter_from(0) if e["kind"] == "step_completed"]
    (sc_run1, sc_run2) = all_completed  # exactly 2 step_completed (run 1 + run 2)
    assert sc_run1["result"] == {"results": []}
    assert sc_run2["result"] == {"results": ["fresh", "data"]}
