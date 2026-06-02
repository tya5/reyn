"""Tier 2: #1212 PR5 — op-loop resume semantics (decision B).

PR5 decision (B): the op-loop accepts re-decide-on-resume (``call_tools`` is not
LLM-memoized) but the op-EXECUTION layer stays WAL-protected — ``dispatch_tool``
memoizes each op on (op_invocation_id + args_hash), so a *deterministic*
re-decide (the model re-emits the same op) memo-HITS and does NOT re-execute the
side effect. This test pins that guarantee boundary:

  - the op-loop's act-turn op participates in ``dispatch_tool`` resume memo (a
    matching CommittedStep → ``step_memoized``, no re-execution), AND
  - ``call_tools`` IS re-invoked on resume (the act turn is re-decided, not
    replayed) — the documented weaker guarantee that (A), the deterministic
    act-turn memo, is HARD-GATED to land before any production op-loop opt-in.

Real ``OSRuntime`` + real ``ControlIRExecutor`` + real WAL (``StateLog``); the
only scripted seam is the module-level ``call_llm`` / ``call_llm_tools`` provider
boundary (the sanctioned pattern, not a collaborator mock). The ResumePlan is
built from the run-1 WAL committed steps.
"""
from __future__ import annotations

import asyncio

import reyn.kernel.llm_call_recorder as lcr
from reyn.events.state_log import StateLog
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan

_FINISH = {
    "type": "finish",
    "control": {
        "type": "finish", "decision": "finish", "next_phase": None,
        "confidence": 1.0, "reason": {"summary": "done"},
    },
    "artifact": {"type": "result", "data": {}},
}


def _skill() -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["file"],
    )
    return Skill(
        name="op_loop_resume", entry_phase="draft", phases={"draft": draft},
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


class _OpThenStop:
    """Deterministic op-loop script: turn 1 emits a file write op, turn 2 stops
    (→ json decide). Fresh instance per run so the turn counter resets; identical
    output on every run (deterministic re-decide)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        self.calls += 1
        if self.calls == 1:
            return _tool_result([{
                "id": "c1", "type": "function",
                "function": {
                    "name": "file",
                    "arguments": '{"op": "write", "path": "out.txt", "content": "hi"}',
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
    """Build CommittedSteps from the run-1 WAL step_completed entries (= what the
    resume coordinator would reconstruct), so run 2 resumes against them."""
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


def test_op_loop_op_memoized_on_resume_act_turn_re_decided(tmp_path, monkeypatch) -> None:
    """Tier 2: on resume the op-loop re-decides (call_tools re-invoked) but its
    file op memo-HITS through dispatch_tool (step_memoized, no re-execution) —
    decision B's guarantee boundary."""
    monkeypatch.chdir(tmp_path)
    wal = tmp_path / ".reyn" / "wal.jsonl"

    # ── Run 1: op-loop executes the file op, WAL records the step ─────────────
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    script1 = _OpThenStop()
    monkeypatch.setattr(lcr, "call_llm_tools", script1)

    sl1 = StateLog(wal)
    rt1 = OSRuntime(
        _skill(), model="stub/model", run_id="run_op_resume",
        state_log=sl1, tool_calls_op_loop_skills=["op_loop_resume"],
    )
    r1 = asyncio.run(rt1.run({"type": "input", "data": {}}))
    assert r1.ok, f"run 1 must complete; got {r1.status}"
    assert script1.calls >= 1, "op-loop must have called call_tools in run 1"

    committed = _committed_steps_from_wal(sl1)
    op_steps = [s for s in committed if s.op_kind == "file"]
    assert op_steps, f"run 1 WAL must hold a file op step; got {[s.op_kind for s in committed]}"

    # ── Run 2: resume against run-1 steps; same deterministic script ──────────
    sl2 = StateLog(wal)
    plan = ResumePlan(
        run_id="run_op_resume", skill_name="op_loop_resume",
        skill_input={"type": "input", "data": {}}, current_phase="draft",
        last_phase_artifact_path=None, awaiting_intervention_id=None,
        committed_steps=committed,
    )
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    script2 = _OpThenStop()  # fresh counter, identical (deterministic) output
    monkeypatch.setattr(lcr, "call_llm_tools", script2)

    rt2 = OSRuntime(
        _skill(), model="stub/model", run_id="run_op_resume",
        state_log=sl2, resume_plan=plan,
        tool_calls_op_loop_skills=["op_loop_resume"],
    )
    r2 = asyncio.run(rt2.run({"type": "input", "data": {}}))
    assert r2.ok, f"resume must complete; got {r2.status}"

    # The op-EXECUTION layer is WAL-protected: the FILE op (not just the decide
    # llm) memo-hits on resume → deterministic re-decide does not re-execute.
    file_memos = [
        e for e in rt2.events.all()
        if e.type == "step_memoized" and e.data.get("tool") == "file"
    ]
    assert file_memos, (
        "the op-loop's file op must memo-hit dispatch_tool on resume (decision B "
        "safety: a deterministic re-decide does not re-execute the side effect); "
        f"step_memoized events={[e.data.get('tool') or e.data.get('op_kind') for e in rt2.events.all() if e.type == 'step_memoized']}"
    )
    # The documented weaker guarantee: the act turn is RE-DECIDED, not replayed.
    assert script2.calls >= 1, (
        "call_tools must be re-invoked on resume (act turns are not LLM-memoized "
        "— the (A) deterministic act-turn memo is hard-gated for prod enablement)"
    )
