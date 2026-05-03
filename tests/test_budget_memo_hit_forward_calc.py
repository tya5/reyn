"""Tier 2: R-D8 L3 — memo hit forward-calc credits the budget tracker.

When R-D2's LLM memoization hits (recorded result returned without
actually invoking ``call_llm``), the budget tracker must still be
credited with the recorded usage. Without this, post-crash budget
shows lower than actual spend and per-skill / per-agent caps are
bypassed on resume.

Also pins the WAL emit side: ``_wal_step_completed_for_llm`` writes
the usage so a future resume can re-credit.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import reyn.kernel.runtime as runtime_mod
from reyn.budget.budget import BudgetTracker, CostConfig, CostLimitConfig
from reyn.dispatch.dispatcher import _compute_llm_args_hash
from reyn.events.state_log import StateLog
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan


def _one_phase_skill() -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name="memo_budget",
        entry_phase="draft",
        phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


# ---------------------------------------------------------------------------
# L2: WAL emit includes usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wal_step_completed_for_llm_records_usage(tmp_path, monkeypatch):
    """Tier 2: real LLM call emits step_completed with usage data in WAL."""
    monkeypatch.chdir(tmp_path)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = OSRuntime(
        _one_phase_skill(),
        model="stub/model", run_id="run_emit",
        state_log=state_log,
    )

    # Stub call_llm to return known usage
    async def stub_call_llm(model, frame, *args, **kwargs):
        return LLMCallResult(
            data={"type": "finish", "control": {
                "type": "finish", "decision": "finish", "next_phase": None,
                "confidence": 1.0, "reason": {"summary": "d"},
            }, "artifact": {"type": "result", "data": {}}},
            usage=TokenUsage(prompt_tokens=120, completion_tokens=40),
        )
    monkeypatch.setattr(runtime_mod, "call_llm", stub_call_llm)

    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")
    await rt._call_llm_and_record("draft", frame, prior_attempts=None)

    completed = [e for e in state_log.iter_from(0) if e["kind"] == "step_completed"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev.get("usage") is not None, (
        "step_completed for LLM must record usage so memo hit can credit budget"
    )
    assert ev["usage"]["prompt_tokens"] == 120
    assert ev["usage"]["completion_tokens"] == 40


# ---------------------------------------------------------------------------
# L3: memo hit credits the budget tracker
# ---------------------------------------------------------------------------


def _build_args_hash_for(rt: OSRuntime, phase: str = "draft") -> str:
    resolved_model = rt._resolver.resolve(rt._effective_model(phase))
    phase_def = rt.skill.phases.get(phase)
    frame = rt._build_frame(phase, {"type": "input", "data": {}}, [], "en")
    return _compute_llm_args_hash(
        model=resolved_model,
        frame=frame.model_dump(mode="json"),
        prior_attempts=None,
        rollback_context=None,
        system_inputs={
            "skill_name": rt.skill.name,
            "skill_description": rt.skill.description,
            "phase_role": phase_def.role if phase_def else None,
            "project_context": rt._project_context,
            "agent_role": rt._agent_role,
        },
    )


@pytest.mark.asyncio
async def test_memo_hit_with_usage_credits_budget(tmp_path, monkeypatch):
    """Tier 2: memo hit invokes record_llm with the recorded usage.

    Pre-condition: BudgetTracker starts empty. Memo hit returns recorded
    result (no LLM call). Post-condition: tracker.snapshot() reflects
    the recorded usage as if the LLM had been freshly called.
    """
    monkeypatch.chdir(tmp_path)
    # Stub call_llm so a misuse (= memo miss → fresh call) is detected
    fresh_calls = []
    async def stub_call_llm(model, frame, *args, **kwargs):
        fresh_calls.append(1)
        return LLMCallResult(data={}, usage=None)
    monkeypatch.setattr(runtime_mod, "call_llm", stub_call_llm)

    cost_cfg = CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=10000))
    budget = BudgetTracker(cost_cfg)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = OSRuntime(
        _one_phase_skill(),
        model="stub/model", run_id="run_memo_credit",
        state_log=state_log,
        budget_tracker=budget,
        caller="agents/alpha",
    )
    args_hash = _build_args_hash_for(rt)

    plan = ResumePlan(
        run_id="run_memo_credit",
        skill_name="memo_budget",
        skill_input={"type": "input", "data": {}},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[
            CommittedStep(
                op_invocation_id="draft.llm.0",
                op_kind="llm",
                phase="draft",
                args_hash=args_hash,
                seq=10,
                result={"type": "finish", "control": {
                    "type": "finish", "decision": "finish", "next_phase": None,
                    "confidence": 1.0, "reason": {"summary": "d"},
                }, "artifact": {"type": "result", "data": {}}},
                usage={"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
            ),
        ],
    )
    rt._resume_plan = plan

    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")
    raw = await rt._call_llm_and_record("draft", frame, prior_attempts=None)

    # Memo hit — call_llm not invoked
    assert fresh_calls == [], "memo hit must skip call_llm"
    assert raw["type"] == "finish"

    # Budget credited via recorded usage
    snap = budget.snapshot()
    # Per-agent counter populated
    assert snap["agent_tokens"]["alpha"] == 280, (
        f"memo hit must credit budget with recorded usage; got snapshot={snap}"
    )


@pytest.mark.asyncio
async def test_memo_hit_without_usage_skips_credit_gracefully(tmp_path, monkeypatch):
    """Tier 2: pre-R-D8 committed step (usage=None) → log warning, no credit, no error."""
    monkeypatch.chdir(tmp_path)
    fresh_calls = []
    async def stub_call_llm(model, frame, *args, **kwargs):
        fresh_calls.append(1)
        return LLMCallResult(data={}, usage=None)
    monkeypatch.setattr(runtime_mod, "call_llm", stub_call_llm)

    budget = BudgetTracker(CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=10000)))

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = OSRuntime(
        _one_phase_skill(),
        model="stub/model", run_id="run_no_usage",
        state_log=state_log,
        budget_tracker=budget,
        caller="agents/alpha",
    )
    args_hash = _build_args_hash_for(rt)

    plan = ResumePlan(
        run_id="run_no_usage",
        skill_name="memo_budget",
        skill_input={"type": "input", "data": {}},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[
            CommittedStep(
                op_invocation_id="draft.llm.0",
                op_kind="llm",
                phase="draft",
                args_hash=args_hash,
                seq=10,
                result={"type": "finish", "control": {
                    "type": "finish", "decision": "finish", "next_phase": None,
                    "confidence": 1.0, "reason": {"summary": "d"},
                }, "artifact": {"type": "result", "data": {}}},
                # NO usage field — pre-R-D8 step
            ),
        ],
    )
    rt._resume_plan = plan

    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")
    raw = await rt._call_llm_and_record("draft", frame, prior_attempts=None)

    # Memo hit still proceeds (graceful)
    assert fresh_calls == []
    assert raw["type"] == "finish"

    # Budget NOT credited (graceful skip; no error)
    snap = budget.snapshot()
    assert snap["agent_tokens"].get("alpha", 0) == 0


@pytest.mark.asyncio
async def test_memo_hit_with_no_budget_tracker_is_noop(tmp_path, monkeypatch):
    """Tier 2: memo hit with budget_tracker=None doesn't error (web / test paths)."""
    monkeypatch.chdir(tmp_path)
    async def stub_call_llm(model, frame, *args, **kwargs):
        return LLMCallResult(data={}, usage=None)
    monkeypatch.setattr(runtime_mod, "call_llm", stub_call_llm)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = OSRuntime(
        _one_phase_skill(),
        model="stub/model", run_id="run_no_budget",
        state_log=state_log,
        budget_tracker=None,  # no tracker
    )
    args_hash = _build_args_hash_for(rt)
    rt._resume_plan = ResumePlan(
        run_id="run_no_budget", skill_name="memo_budget", skill_input={},
        current_phase="draft", last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[
            CommittedStep(
                op_invocation_id="draft.llm.0", op_kind="llm",
                phase="draft", args_hash=args_hash, seq=10,
                result={"type": "finish", "control": {
                    "type": "finish", "decision": "finish", "next_phase": None,
                    "confidence": 1.0, "reason": {"summary": "d"},
                }, "artifact": {"type": "result", "data": {}}},
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            ),
        ],
    )

    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")
    # Must not raise
    raw = await rt._call_llm_and_record("draft", frame, prior_attempts=None)
    assert raw["type"] == "finish"
