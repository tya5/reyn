"""Tier 2: #1092 PR-C-2.5 — the CONVERGED op-loop serves the crash-resume
op-dispatch WAL-memoization HARD GATE (json-mode-equal, #1225 Decision A).

Background (the #1128 gap this PR closes). The converged op-loop dispatches
phase ops through ``RouterLoop._execute_tool`` → ``dispatch_tool``. On main that
call passed ``caller_kind="router"`` with no ``state_log`` / ``skill_run_id`` /
``op_invocation_id``, so phase ops wrote NO WAL step — the converged path did not
memoize op dispatches for crash resume (empirically: ``resume_memo_1212`` failed
on the converged path with WAL = ``[llm, llm, llm]``, no op step). Retiring the
frame-fed ``_run_op_loop`` (which DID serve this via the shared
``control_ir_executor``) would therefore drop the PR5 HARD GATE.

The fix (PR-C-2.5): ``RouterLoop._execute_tool`` consults a host hook
(``op_dispatch_memo()``). A phase host returns the per-phase WAL wiring so the
dispatch threads ``state_log`` + ``skill_run_id`` + ``resume_plan`` +
``op_invocation_id`` (``caller_kind="skill_phase"``); chat hosts don't implement
the hook (getattr → None), so the chat dispatch path is byte-identical.

This test pins the restored guarantee end-to-end through the real OSRuntime:
  - run 1: a converged op-loop write_file op writes a ``step_completed`` WAL step
    (the PORT's effect — absent it, no op step → falsifies); AND
  - run 2 (resume): the op memo-HITS ``dispatch_tool`` (``step_memoized``,
    ``tool=write_file``) so the side effect does NOT re-execute, AND the act-turn
    ``call_tools`` memo-HITS (replayed deterministically, not re-invoked).

Real OSRuntime + real ControlIRExecutor + real WAL (StateLog); the only scripted
seam is the module-level ``call_llm`` / ``call_llm_tools`` provider boundary (the
sanctioned pattern). The ``routerloop_convergence_skills`` gate routes this skill
to the converged op-loop (on this branch the two gates still co-exist; #1092 PR-C-3
merges them).

Falsification: reverting the ``op_dispatch_memo`` host hook + the
``RouterLoop._execute_tool`` phase-memo branch makes run 1's WAL hold no op step
(``op_steps`` empty) → this test FAILS, proving it gates on the port.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import reyn.kernel.llm_call_recorder as lcr
import reyn.schemas.models as _models
from reyn.events.state_log import StateLog
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan

_SKILL_NAME = "converged_resume"
# A path under the default ``.reyn/`` write zone — no PermissionDecl needed, so
# the write COMPLETES (a ``step_completed`` WAL step, the memoizable shape) rather
# than being denied (which would record ``step_failed`` and not memo on resume).
_WRITE_PATH = ".reyn/converged_resume.txt"

class _FrozenInstant(_dt.datetime):
    def astimezone(self, tz=None):  # noqa: ANN001, ANN201
        return self


_FROZEN = _FrozenInstant(2026, 1, 1, tzinfo=_dt.timezone.utc)


class _FrozenClock(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return _FROZEN


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
    """Deterministic converged op-loop script: turn 1 emits a write_file op,
    turn 2 stops (→ FD2 json decide). Fresh instance per run so the turn counter
    resets; identical output on every run (deterministic replay)."""

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


def test_converged_op_dispatch_memoized_on_resume(tmp_path, monkeypatch) -> None:
    """Tier 2: on resume a converged op-loop op memo-HITS dispatch_tool (no
    re-execution) and its act turn replays deterministically — the json-mode-equal
    crash-resume HARD GATE, now served by the converged path (PR-C-2.5)."""
    monkeypatch.chdir(tmp_path)
    # Freeze current_datetime so the act-turn LLM memo key is stable across the
    # run-1 / run-2 (resume) boundary — a real crash-resume happens at a later
    # wall-clock time, so this isolates the op-dispatch memo (the PR-C-2.5 subject)
    # from the orthogonal question of whether the LLM-call memo key is
    # datetime-robust (tracked separately).
    monkeypatch.setattr(_models, "datetime", _FrozenClock)
    wal = tmp_path / ".reyn" / "wal.jsonl"

    # ── Run 1: converged op-loop executes the write_file op; WAL records it ───────
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    script1 = _WriteThenStop()
    monkeypatch.setattr(lcr, "call_llm_tools", script1)

    sl1 = StateLog(wal)
    rt1 = OSRuntime(
        _skill(), model="stub/model", run_id="converged_resume_run",
        state_log=sl1, routerloop_convergence_skills=[_SKILL_NAME],
    )
    r1 = asyncio.run(rt1.run({"type": "input", "data": {}}))
    assert r1.ok, f"run 1 must complete; got {r1.status}"
    assert script1.calls >= 1, "converged op-loop must have called call_tools in run 1"

    committed = _committed_steps_from_wal(sl1)
    op_steps = [s for s in committed if s.op_kind == "write_file"]
    # The PORT's effect: the converged op dispatch wrote a WAL op step. Absent the
    # op_dispatch_memo hook + the _execute_tool phase-memo branch, this is empty
    # (the #1128 gap) — so this assertion falsifies the fix.
    assert op_steps, (
        "run 1 WAL must hold a write_file op step (the converged op-dispatch WAL "
        f"memoization PR-C-2.5 restores); got {[s.op_kind for s in committed]}"
    )

    # ── Run 2: resume against run-1 steps; same deterministic script ─────────────
    sl2 = StateLog(wal)
    plan = ResumePlan(
        run_id="converged_resume_run", skill_name=_SKILL_NAME,
        skill_input={"type": "input", "data": {}}, current_phase="draft",
        last_phase_artifact_path=None, awaiting_intervention_id=None,
        committed_steps=committed,
    )
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    script2 = _WriteThenStop()  # fresh counter, identical (deterministic) output
    monkeypatch.setattr(lcr, "call_llm_tools", script2)

    rt2 = OSRuntime(
        _skill(), model="stub/model", run_id="converged_resume_run",
        state_log=sl2, resume_plan=plan,
        routerloop_convergence_skills=[_SKILL_NAME],
    )
    r2 = asyncio.run(rt2.run({"type": "input", "data": {}}))
    assert r2.ok, f"resume must complete; got {r2.status}"

    # (A) the act-turn LLM call replays deterministically: call_tools is NOT
    # re-invoked on resume (memo-HIT returns the recorded tool_calls).
    assert script2.calls == 0, (
        "call_tools must memo-HIT on resume (act turn replayed deterministically); "
        f"got {script2.calls} fresh invocations"
    )
    # (B) the op the replayed act turn produced also memo-hits dispatch_tool → the
    # side-effecting write does NOT re-execute (json-mode-equal crash recovery).
    file_memos = [
        e for e in rt2.events.all()
        if e.type == "step_memoized" and e.data.get("tool") == "write_file"
    ]
    assert file_memos, (
        "the converged op-loop's write_file op must memo-hit dispatch_tool on "
        "resume (deterministic replay does not re-execute the side effect); "
        f"step_memoized={[e.data.get('tool') for e in rt2.events.all() if e.type == 'step_memoized']}"
    )
