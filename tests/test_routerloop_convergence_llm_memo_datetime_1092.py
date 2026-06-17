"""Tier 2: #1092 PR-C-2.6 — the converged op-loop's act-turn LLM memo is
datetime-robust, so a REAL later-time crash-resume HITS (no re-invoke).

The gap (#1128, part 2/2). The converged op-loop's act-turn LLM call is memoized
through RouterLoop's ``compute_sub_loop_args_hash(messages)`` seam. But the
converged op-loop's seed ``user`` message embeds the whole ContextFrame as JSON
(``build_phase_messages`` → ``json.dumps(frame.model_dump())``), INCLUDING the
volatile ``current_datetime``. A real crash-resume happens at a LATER wall-clock
time, so the raw-message hash differs → the act-turn memo MISSES → the LLM
re-invokes → a non-deterministic model can diverge → the op re-executes (the PR5
json-mode-equal HARD GATE breaks). The frame-fed op-loop avoided this by hashing
the FRAME with volatile fields stripped (``_compute_llm_args_hash`` /
``_LLM_VOLATILE_FRAME_FIELDS``).

The fix (PR-C-2.6): ``RouterLoop`` host-delegates the memo key when the host
implements ``compute_memo_key`` (``PhaseRouterLoopHost`` strips the SAME volatile
frame fields from the seed message before hashing). Chat hosts don't implement it
(getattr → None) → the chat memo key is byte-identical.

This test pins the essential gate: run 1 records at datetime D1; run 2 RESUMES at a
DIFFERENT datetime D2 (≠ D1, simulating a later-time resume); the act-turn
``call_tools`` MUST memo-HIT (not re-invoke) despite the datetime change.

Falsification: reverting the host hook (RouterLoop falls back to the raw-message
``compute_sub_loop_args_hash``) makes run 2's D2 hash differ from run 1's D1 hash →
the memo MISSES → ``call_tools`` re-invokes → this test FAILS, proving it gates on
the datetime-robust key.

★ C-3 RETIRE GATE: with BOTH PR-C-2.5 (op-dispatch WAL memo) and PR-C-2.6
(datetime-robust LLM memo) on main, this asserts the FULL json-mode-equal
crash-resume — on a later-time resume the act turn memo-HITS (no re-invoke) AND
the op memo-HITS (no re-execution). That combined property is what gates retiring
the frame-fed ``_run_op_loop``: the converged op-loop now serves the same
crash-recovery guarantee the retired path did.

Real OSRuntime + real ControlIRExecutor + real WAL; the only scripted seam is the
module-level ``call_llm`` / ``call_llm_tools`` provider boundary.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import reyn.core.kernel.llm_call_recorder as lcr
import reyn.schemas.models as _models
from reyn.core.events.state_log import StateLog
from reyn.core.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan

_SKILL_NAME = "converged_dt_resume"
_WRITE_PATH = ".reyn/converged_dt.txt"  # default write zone — no decl needed

_FINISH = {
    "type": "finish",
    "control": {
        "type": "finish", "decision": "finish", "next_phase": None,
        "confidence": 1.0, "reason": {"summary": "done"},
    },
    "artifact": {"type": "result", "data": {}},
}


def _frozen_clock_at(instant: "_dt.datetime"):
    """A drop-in for ``reyn.schemas.models.datetime`` whose ``now()`` returns a
    FIXED instant — used to give run 1 and run 2 DIFFERENT current_datetimes."""

    class _Instant(_dt.datetime):
        def astimezone(self, tz=None):  # noqa: ANN001, ANN201
            return self

    fixed = _Instant(
        instant.year, instant.month, instant.day, instant.hour, instant.minute,
        tzinfo=_dt.timezone.utc,
    )

    class _Clock(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return fixed

    return _Clock


# Two DIFFERENT wall-clock instants: run 1 at T1, the (later-time) resume at T2.
_T1 = _dt.datetime(2026, 1, 1, 9, 0, tzinfo=_dt.timezone.utc)
_T2 = _dt.datetime(2026, 1, 1, 17, 30, tzinfo=_dt.timezone.utc)  # ≠ T1 (8.5h later)


def _skill() -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["write_file"],
    )
    return Skill(
        name=_SKILL_NAME, entry_phase="draft", phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _tool_result(tool_calls: list) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=None, tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
    )


class _WriteThenStop:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        self.calls += 1
        if self.calls == 1:
            return _tool_result([{
                "id": "c1", "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path": "%s", "content": "hi"}' % _WRITE_PATH,
                },
            }])
        return _tool_result([])


def _finish_llm():
    async def _f(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(
            data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10),
        )
    return _f


def _committed_steps_from_wal(state_log: StateLog) -> list[CommittedStep]:
    steps: list[CommittedStep] = []
    for e in state_log.iter_from(0):
        if e["kind"] != "step_completed":
            continue
        steps.append(CommittedStep(
            op_invocation_id=e["op_invocation_id"],
            op_kind=e.get("op_kind", ""),
            phase=e["phase"],
            args_hash=e.get("args_hash", ""),
            seq=e.get("seq", 0),
            result=e.get("result"),
            usage=e.get("usage"),
        ))
    return steps


def test_converged_act_turn_memo_hits_across_datetime_change(tmp_path, monkeypatch) -> None:
    """Tier 2: the converged op-loop act turn memo-HITS on a later-time resume
    (run1@T1 → resume@T2≠T1) — the datetime-robust memo key (PR-C-2.6)."""
    monkeypatch.chdir(tmp_path)
    wal = tmp_path / ".reyn" / "wal.jsonl"

    # ── Run 1 at T1: record the act-turn LLM steps (keyed on the robust hash) ─────
    monkeypatch.setattr(_models, "datetime", _frozen_clock_at(_T1))
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    script1 = _WriteThenStop()
    monkeypatch.setattr(lcr, "call_llm_tools", script1)

    sl1 = StateLog(wal)
    rt1 = OSRuntime(
        _skill(), model="stub/model", run_id="converged_dt_run",
        state_log=sl1, tool_calls_op_loop_skills=[_SKILL_NAME],
    )
    r1 = asyncio.run(rt1.run({"type": "input", "data": {}}))
    assert r1.ok, f"run 1 must complete; got {r1.status}"
    assert script1.calls >= 1, "run 1 must have invoked call_tools"

    committed = _committed_steps_from_wal(sl1)
    llm_steps = [s for s in committed if s.op_kind == "llm"]
    assert llm_steps, (
        f"run 1 WAL must hold act-turn llm steps to resume against; got "
        f"{[s.op_kind for s in committed]}"
    )

    # ── Run 2 at T2 (≠ T1): resume; the act turn must memo-HIT despite the
    #    datetime change (the robust key ignores current_datetime) ────────────────
    monkeypatch.setattr(_models, "datetime", _frozen_clock_at(_T2))
    sl2 = StateLog(wal)
    plan = ResumePlan(
        run_id="converged_dt_run", skill_name=_SKILL_NAME,
        skill_input={"type": "input", "data": {}}, current_phase="draft",
        last_phase_artifact_path=None, awaiting_intervention_id=None,
        committed_steps=committed,
    )
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    script2 = _WriteThenStop()  # fresh counter; re-invocation would advance it
    monkeypatch.setattr(lcr, "call_llm_tools", script2)

    rt2 = OSRuntime(
        _skill(), model="stub/model", run_id="converged_dt_run",
        state_log=sl2, resume_plan=plan,
        tool_calls_op_loop_skills=[_SKILL_NAME],
    )
    r2 = asyncio.run(rt2.run({"type": "input", "data": {}}))
    assert r2.ok, f"resume must complete; got {r2.status}"

    # THE GATE: the act-turn call_tools memo-HITS at the DIFFERENT resume datetime —
    # it is NOT re-invoked. Without the datetime-robust key (falsification: revert
    # the host hook), run 2's T2 message hash differs from run 1's T1 hash → MISS →
    # call_tools re-invokes → script2.calls > 0.
    assert script2.calls == 0, (
        "the converged act-turn call_tools must memo-HIT on a later-time resume "
        "(datetime-robust memo key); it was re-invoked "
        f"{script2.calls}x — the current_datetime change broke the memo key"
    )
    llm_memos = [
        e for e in rt2.events.all()
        if e.type == "step_memoized" and e.data.get("op_kind") == "llm"
    ]
    assert llm_memos, (
        "the act-turn llm call must emit step_memoized on resume (memo-HIT); "
        f"got step_memoized op_kinds={[e.data.get('op_kind') for e in rt2.events.all() if e.type == 'step_memoized']}"
    )
    # ★ FULL json-mode-equal crash-resume (the C-3 retire gate). With BOTH PR-C-2.5
    # (op-dispatch WAL memo) and PR-C-2.6 (datetime-robust LLM memo) on main, the
    # write_file op the replayed act turn produced ALSO memo-HITS dispatch_tool on
    # the later-time resume — so the side-effecting op does NOT re-execute. This is
    # the property that gates retiring the frame-fed _run_op_loop: the converged
    # op-loop serves the same crash-recovery guarantee end-to-end (act-turn replay
    # determinism AND op no-re-execution) across a real wall-clock-advancing resume.
    op_memos = [
        e for e in rt2.events.all()
        if e.type == "step_memoized" and e.data.get("tool") == "write_file"
    ]
    assert op_memos, (
        "the converged op-loop's write_file op must memo-HIT dispatch_tool on a "
        "later-time resume (no re-execution) — the full json-mode-equal crash-resume "
        f"gate; step_memoized tools={[e.data.get('tool') for e in rt2.events.all() if e.type == 'step_memoized']}"
    )
