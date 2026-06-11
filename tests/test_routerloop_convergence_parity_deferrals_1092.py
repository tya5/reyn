"""Tier 2: #1092 PR-C-5 — the converged op-loop serves the PR2-deferrals at
json-mode-parity (verify-before-retire / serve-before-retire, #1128).

Three deferrals the converged op-loop did NOT serve (the frame-fed _run_op_loop
"PR2-scope simplifications vs _run_act_loop") are ported / fixed here; this pins
each with a #1128 gate (wiring + sufficiency + falsification):

(2) handle_limit_exceeded — per-turn phase wall-clock budget enforcement. The
    converged op-loop now runs the SAME _check_phase_budget json-mode runs before
    each call_llm, via a run_loop host-hook. (safety-critical.)
(3) rollback-reason injection — the rollback rejection feedback now reaches the
    converged op-loop seed (not just the FD2 decide).
(4) force-close effective_act_turn_cap — the converged op-loop uses the
    resume-adjusted effective cap, not the raw max_act_turns.

(1) on-demand compact op is documented obviated by C-4b auto-compaction (no port).

Real OSRuntime + real PhaseExecutor; the only scripted seam is the module-level
call_llm / call_llm_tools provider boundary.
"""
from __future__ import annotations

import asyncio

import pytest

import reyn.kernel.llm_call_recorder as lcr
import reyn.kernel.run_state as run_state
from reyn.config import OnLimitConfig, SafetyConfig, TimeoutConfig
from reyn.kernel.run_state import RunState
from reyn.kernel.runtime import OSRuntime
from reyn.kernel.runtime_types import PhaseBudgetExceededError
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import CandidateOutput, Phase, Skill, SkillGraph

_SKILL_NAME = "converged_deferrals"

_FINISH = {
    "type": "finish",
    "control": {
        "type": "finish", "decision": "finish", "next_phase": None,
        "confidence": 1.0, "reason": {"summary": "done"},
    },
    "artifact": {"type": "result", "data": {}},
}


def _skill(allowed_ops=None) -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=allowed_ops or [],
    )
    return Skill(
        name=_SKILL_NAME, entry_phase="draft", phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _stop_tools():
    async def _t(*a, **k):  # noqa: ANN002, ANN003
        return LLMToolCallResult(
            content=None, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )
    return _t


def _finish_llm():
    async def _f(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))
    return _f


# ── (2) handle_limit_exceeded: per-turn phase budget enforcement ────────────────


def test_converged_op_loop_enforces_phase_budget(tmp_path, monkeypatch) -> None:
    """Tier 2: the converged op-loop enforces the per-turn phase wall-clock budget
    (C-5 deferral 2). Over budget with on_limit=unattended → PhaseBudgetExceededError,
    the SAME enforcement json-mode runs (full, not a degraded stand-in).

    Falsification: reverting the run_loop check_phase_budget hook makes the over-budget
    converged run complete normally (no enforcement) — verified locally.
    """
    monkeypatch.chdir(tmp_path)
    # Deterministic over-budget clock: begin_phase records phase_started_at on the
    # first monotonic() call; every later call jumps +10000s so elapsed >> budget.
    _ticks = {"n": 0}

    def _fake_monotonic():
        v = _ticks["n"] * 10000.0
        _ticks["n"] += 1
        return v
    monkeypatch.setattr(run_state.time, "monotonic", _fake_monotonic)

    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _stop_tools())

    safety = SafetyConfig(
        timeout=TimeoutConfig(phase_seconds=1.0),
        on_limit=OnLimitConfig(mode="unattended"),  # over-budget → immediate abort/raise
    )
    rt = OSRuntime(
        _skill(), model="stub/model", run_id="convbudget",
        tool_calls_op_loop_skills=[_SKILL_NAME], safety=safety,
    )
    # The per-turn budget hook fires at the top of the op-loop's first iteration
    # (before call_llm_tools); elapsed (10000s) >> 1s budget → _check_phase_budget
    # raises PhaseBudgetExceededError, which the orchestrator surfaces as a
    # ``phase_budget_exceeded`` RunResult (FP-0005). Without the hook the run would
    # finish normally (no enforcement) — the falsification.
    result = asyncio.run(rt.run({"type": "input", "data": {}}))
    assert result.status == "phase_budget_exceeded", (
        "the converged op-loop must enforce the per-turn phase budget (C-5 deferral 2); "
        f"got status={result.status!r} (expected phase_budget_exceeded)"
    )


# ── (3) rollback-reason injection into the converged op-loop seed ────────────────


def test_converged_op_loop_seed_surfaces_rollback_reason(tmp_path, monkeypatch) -> None:
    """Tier 2: the rollback rejection reason reaches the converged op-loop SEED
    (C-5 deferral 3) — so the op-loop adapts its op-gathering, not just the FD2
    decide. json-mode injects it into the act frame via call_llm's rollback_context;
    the converged op-loop turns don't carry rollback_context, so the port appends the
    feedback to the seed. Driven by a direct ``_run_routerloop_op_loop`` call with a
    rollback_context (parallel to the json-mode rollback test that calls
    ``_run_act_loop`` directly).

    Falsification: reverting the seed-injection makes the reason absent from the
    op-loop seed (it would then appear only in the FD2 decide).
    """
    monkeypatch.chdir(tmp_path)
    _REASON = "the artifact omitted the required citations"
    captured = {"seed": None}

    async def _capture_tools(*a, **k):  # noqa: ANN002, ANN003
        if captured["seed"] is None:
            captured["seed"] = k.get("messages", [])
        return LLMToolCallResult(
            content=None, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _capture_tools)

    rt = OSRuntime(
        _skill(), model="stub/model", run_id="convrollback",
        tool_calls_op_loop_skills=[_SKILL_NAME],
    )
    state = rt._state
    state.begin_phase("draft")
    cand = CandidateOutput(
        next_phase="draft", control_type="transition", schema_name="result",
        artifact_schema={"type": "object", "properties": {}},
    )
    rollback_context = {
        "rejected_artifact": {"type": "result", "data": {}},
        "reason": _REASON,
        "rollback_from": "review",
        "previous_control_ir_results": [],
    }
    asyncio.run(rt._phase_executor._run_routerloop_op_loop(
        "draft", {"type": "input", "data": {}}, [cand], None,
        2, 2, None, state, rollback_context=rollback_context,
    ))

    seed_blob = str(captured["seed"])
    assert _REASON in seed_blob, (
        "the converged op-loop seed must surface the rollback reason (C-5 deferral 3) "
        f"so the op-loop adapts; reason {_REASON!r} not in seed messages={captured['seed']!r}"
    )


# ── (4) force-close uses the effective (resume-adjusted) act-turn cap ────────────


def test_converged_op_loop_uses_effective_act_turn_cap(tmp_path, monkeypatch) -> None:
    """Tier 2: the converged op-loop force-closes at the EFFECTIVE act-turn cap, not
    the raw max_act_turns (C-5 deferral 4, json-mode-parity). A monkeypatched
    effective cap (3) below the phase's raw max_act_turns (10) bounds the op-loop to
    3 act turns.

    Falsification: reverting the fix (raw max_act_turns) runs up to 10 turns.
    """
    monkeypatch.chdir(tmp_path)
    _EFFECTIVE = 3
    monkeypatch.setattr(
        RunState, "effective_act_turn_cap",
        lambda self, phase, base_cap: _EFFECTIVE,
    )

    calls = {"n": 0}

    async def _never_stop(*a, **k):  # noqa: ANN002, ANN003
        # Always emit a read_file tool_call (continue) so the op-loop never
        # voluntarily ends — it runs until max_iterations (= the effective cap)
        # bounds it, exposing whether that cap is effective (3) or raw (10).
        i = calls["n"]
        calls["n"] += 1
        return LLMToolCallResult(
            content=None,
            tool_calls=[{
                "id": f"c{i}", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "x.txt"}'},
            }],
            finish_reason="tool_calls",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _never_stop)

    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["read_file"], max_act_turns=10,  # raw cap 10; effective (patched) = 3
    )
    skill = Skill(
        name=_SKILL_NAME, entry_phase="draft", phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )
    rt = OSRuntime(
        skill, model="stub/model", run_id="convcap",
        tool_calls_op_loop_skills=[_SKILL_NAME],
    )
    asyncio.run(rt.run({"type": "input", "data": {}}))
    # The op-loop (always emitting tool_calls) ran exactly the EFFECTIVE cap (3),
    # not the raw 10 — proving converged force-close honors the resume-adjusted cap.
    # _EFFECTIVE loop iterations + 1 force-close wrap-up call (#1496).
    assert calls["n"] == _EFFECTIVE + 1, (
        "the converged op-loop must bound iterations by the EFFECTIVE act-turn cap "
        f"({_EFFECTIVE}), not the raw max_act_turns (10); expected {_EFFECTIVE + 1} "
        f"calls ({_EFFECTIVE} loop + 1 force-close wrap-up), got {calls['n']}x"
    )
