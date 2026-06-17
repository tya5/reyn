"""Tier 2: LLMCallRecorder invariants — memo lookup, WAL recording, budget.

FP-0020 Component B. Guards three core guarantees of LLMCallRecorder:

1. test_memo_hit_skips_llm_call
   On resume with a matching CommittedStep, call_llm is NOT invoked and
   the recorded result is returned. Budget is credited from the recorded
   usage. (memo-hit path)

2. test_wal_records_after_llm_call
   After a fresh LLM call, a step_completed entry is written to the WAL
   with the correct phase, op_invocation_id, and usage data so a future
   resume can memo-hit.

3. test_budget_post_record_increments_usage
   After a fresh LLM call that returns usage data, the BudgetTracker's
   per-agent token counter is incremented and state.token_usage is updated.

All tests exercise LLMCallRecorder through OSRuntime's public surface
(_call_llm_and_record shim) with call_llm replaced by a plain async
callable (no AsyncMock / MagicMock). Real BudgetTracker, real StateLog,
real RunState.
"""
from __future__ import annotations

import pytest

import reyn.core.kernel.llm_call_recorder as llm_call_recorder_mod
from reyn.budget.budget import BudgetTracker, CostConfig, CostLimitConfig
from reyn.core.dispatch.dispatcher import _compute_llm_args_hash
from reyn.core.events.state_log import StateLog
from reyn.core.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan

# ── Helpers ─────────────────────────────────────────────────────────────────


def _one_phase_skill() -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name="recorder_inv",
        entry_phase="draft",
        phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


_FINISH_RESULT = {
    "type": "finish",
    "control": {
        "type": "finish", "decision": "finish", "next_phase": None,
        "confidence": 1.0, "reason": {"summary": "done"},
    },
    "artifact": {"type": "result", "data": {}},
}


def _make_args_hash(skill: Skill, phase: str = "draft", model: str = "stub/model") -> str:
    """Compute the args_hash that OSRuntime produces for the first LLM call."""
    rt_tmp = OSRuntime(skill, model=model, run_id="_hash_tmp")
    frame = rt_tmp.build_frame(phase, {"type": "input", "data": {}}, [], "en")
    phase_def = skill.phases.get(phase)
    return _compute_llm_args_hash(
        model=model,
        frame=frame.model_dump(mode="json"),
        prior_attempts=None,
        rollback_context=None,
        system_inputs={
            "skill_name": skill.name,
            "skill_description": skill.description,
            "phase_role": phase_def.role if phase_def else None,
            "project_context": "",
            "agent_role": "",
        },
    )


# ── Test 1: memo hit skips call_llm ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_memo_hit_skips_llm_call(tmp_path, monkeypatch):
    """Tier 2: LLMCallRecorder — matching memo skips call_llm, credits budget.

    Pre-condition: a ResumePlan with one CommittedStep matching the first
    LLM call's (op_invocation_id, args_hash) is supplied at construction.
    Post-condition:
      - call_llm is NOT invoked (recorded result returned instead).
      - BudgetTracker's per-agent token counter reflects the recorded usage.
    """
    monkeypatch.chdir(tmp_path)

    fresh_calls: list[int] = []

    async def stub_call_llm(model, frame, *args, **kwargs):
        fresh_calls.append(1)
        return LLMCallResult(data={}, usage=None)

    monkeypatch.setattr(llm_call_recorder_mod, "call_llm", stub_call_llm)

    skill = _one_phase_skill()
    args_hash = _make_args_hash(skill)

    plan = ResumePlan(
        run_id="run_memo_skip",
        skill_name="recorder_inv",
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
                seq=1,
                result=_FINISH_RESULT,
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            ),
        ],
    )

    budget = BudgetTracker(
        CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=10_000))
    )
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = OSRuntime(
        skill,
        model="stub/model",
        run_id="run_memo_skip",
        state_log=state_log,
        budget_tracker=budget,
        caller="agents/test-agent",
        resume_plan=plan,
    )

    frame = rt.build_frame("draft", {"type": "input", "data": {}}, [], "en")
    raw = await rt._call_llm_and_record("draft", frame, prior_attempts=None)

    # Memo hit — LLM not invoked
    assert fresh_calls == [], "memo hit must not invoke call_llm"
    assert raw["type"] == "finish"

    # Budget credited from recorded usage
    snap = budget.snapshot()
    assert snap["agent_tokens"]["test-agent"] == 150, (
        f"memo hit must credit budget with recorded usage; snapshot={snap}"
    )


# ── Test 2: WAL records usage after fresh LLM call ──────────────────────────


@pytest.mark.asyncio
async def test_wal_records_after_llm_call(tmp_path, monkeypatch):
    """Tier 2: LLMCallRecorder — fresh call writes step_completed with usage.

    Pre-condition: no resume_plan (fresh run). A stub call_llm returns
    known usage data.
    Post-condition: the WAL contains exactly one step_completed entry of
    op_kind='llm' with the expected usage dict so a future resume can
    credit the budget.
    """
    monkeypatch.chdir(tmp_path)

    async def stub_call_llm(model, frame, *args, **kwargs):
        return LLMCallResult(
            data=_FINISH_RESULT,
            usage=TokenUsage(prompt_tokens=80, completion_tokens=30),
        )

    monkeypatch.setattr(llm_call_recorder_mod, "call_llm", stub_call_llm)

    skill = _one_phase_skill()
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = OSRuntime(
        skill,
        model="stub/model",
        run_id="run_wal_write",
        state_log=state_log,
    )

    frame = rt.build_frame("draft", {"type": "input", "data": {}}, [], "en")
    await rt._call_llm_and_record("draft", frame, prior_attempts=None)

    completed = [
        e for e in state_log.iter_from(0)
        if e["kind"] == "step_completed" and e.get("op_kind") == "llm"
    ]
    assert completed, "at least one step_completed(llm) must be written after a fresh LLM call"
    (ev,) = completed
    assert ev["phase"] == "draft"
    assert ev["op_invocation_id"] == "draft.llm.0"
    usage = ev.get("usage")
    assert usage is not None, "step_completed must carry usage for future budget credit"
    assert usage["prompt_tokens"] == 80
    assert usage["completion_tokens"] == 30


# ── Test 3: budget tracker and state.token_usage incremented after call ──────


@pytest.mark.asyncio
async def test_budget_post_record_increments_usage(tmp_path, monkeypatch):
    """Tier 2: LLMCallRecorder — fresh LLM call increments budget + RunState.

    Pre-condition: BudgetTracker starts empty; RunState.token_usage is zero.
    A fresh call_llm returns usage data.
    Post-condition:
      - BudgetTracker.snapshot()['agent_tokens'] reflects the call's usage.
      - OSRuntime._state.token_usage (= RunState.token_usage) is non-zero.
    """
    monkeypatch.chdir(tmp_path)

    async def stub_call_llm(model, frame, *args, **kwargs):
        return LLMCallResult(
            data=_FINISH_RESULT,
            usage=TokenUsage(prompt_tokens=200, completion_tokens=60),
        )

    monkeypatch.setattr(llm_call_recorder_mod, "call_llm", stub_call_llm)

    skill = _one_phase_skill()
    budget = BudgetTracker(
        CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=100_000))
    )
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = OSRuntime(
        skill,
        model="stub/model",
        run_id="run_budget_post",
        budget_tracker=budget,
        caller="agents/beta",
        state_log=state_log,
    )

    frame = rt.build_frame("draft", {"type": "input", "data": {}}, [], "en")
    await rt._call_llm_and_record("draft", frame, prior_attempts=None)

    # BudgetTracker incremented
    snap = budget.snapshot()
    assert snap["agent_tokens"]["beta"] == 260, (
        f"budget_tracker must record post-LLM usage; snapshot={snap}"
    )

    # RunState token_usage incremented (public snapshot via _state)
    usage = rt._state.token_usage
    assert usage.prompt_tokens == 200
    assert usage.completion_tokens == 60
    assert usage.total_tokens == 260
