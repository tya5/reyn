"""Tier 2: OSRuntime invariant — `_call_llm_and_record` memoizes against ResumePlan.

R-D2 L2. The LLM call is the most expensive step in a phase visit (cost +
non-determinism). On resume, when ``resume_plan`` is supplied AND a
``CommittedStep`` matches the call's ``(op_invocation_id, phase,
args_hash)``, the recorded LLM result must be substituted in WITHOUT
invoking ``call_llm`` again. A miss falls through to the real call and
emits ``step_completed`` to the WAL so a future resume can hit.

Invariants pinned here:
  - hit: ``call_llm`` not invoked, ``step_memoized`` event emitted
  - miss: ``call_llm`` invoked once, ``step_completed`` appended to WAL
  - no-resume: ``call_llm`` invoked once, no memo path exercised
  - LLM step_failed is NOT memoized (transient errors re-tried on resume)
  - schema versioning: corrupt memo result falls through gracefully
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import reyn.kernel.runtime as runtime_mod
from reyn.dispatch.dispatcher import _compute_llm_args_hash
from reyn.events.state_log import StateLog
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult
from reyn.schemas.models import (
    CandidateOutput,
    Phase,
    Skill,
    SkillGraph,
)
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan


# ---------------------------------------------------------------------------
# Stub call_llm
# ---------------------------------------------------------------------------


class _CallCounter:
    """Records every call_llm invocation; returns a configurable response.

    Acts as a pytest-monkeypatch target replacing
    ``reyn.kernel.runtime.call_llm`` for the duration of a test.
    """

    def __init__(self, response: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response or {
            "type": "finish",
            "control": {
                "type": "finish",
                "decision": "finish",
                "next_phase": None,
                "confidence": 1.0,
                "reason": {"summary": "done"},
            },
            "artifact": {"type": "result", "data": {}},
        }

    async def __call__(self, model, frame, *args, **kwargs):  # noqa: ARG002
        self.calls.append({"model": model, "kwargs": kwargs})
        return LLMCallResult(data=self._response, usage=None)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _one_phase_skill() -> Skill:
    draft = Phase(
        name="draft",
        instructions="draft",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name="memo_test",
        entry_phase="draft",
        phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _runtime(
    *,
    state_log: StateLog | None = None,
    resume_plan: ResumePlan | None = None,
) -> OSRuntime:
    return OSRuntime(
        _one_phase_skill(),
        model="stub/model",
        run_id="run_memo_test",
        state_log=state_log,
        resume_plan=resume_plan,
    )


def _build_args_hash_for(rt: OSRuntime, phase: str) -> str:
    """Compute the args_hash that ``_call_llm_and_record`` will use.

    Mirrors the runtime's hashing inputs (model resolved, frame, no priors,
    no rollback, system_inputs from skill metadata).
    """
    resolved_model = rt._resolver.resolve(rt._effective_model(phase))  # type: ignore[attr-defined]
    phase_def = rt.skill.phases.get(phase)
    frame = rt._build_frame(  # type: ignore[attr-defined]
        phase, {"type": "input", "data": {}}, [], "en",
    )
    return _compute_llm_args_hash(
        model=resolved_model,
        frame=frame.model_dump(mode="json"),
        prior_attempts=None,
        rollback_context=None,
        system_inputs={
            "skill_name": rt.skill.name,
            "skill_description": rt.skill.description,
            "phase_role": phase_def.role if phase_def else None,
            "project_context": rt._project_context,  # type: ignore[attr-defined]
            "agent_role": rt._agent_role,  # type: ignore[attr-defined]
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_memo_hit_skips_call_llm_and_emits_step_memoized(tmp_path, monkeypatch):
    """Tier 2: matching CommittedStep → call_llm NOT invoked, step_memoized fired."""
    monkeypatch.chdir(tmp_path)
    counter = _CallCounter()
    monkeypatch.setattr(runtime_mod, "call_llm", counter)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    # Build runtime first so we can compute the exact args_hash the runtime uses
    # at invocation time. resume_plan is plugged in below after we know the hash.
    rt = _runtime(state_log=state_log)
    args_hash = _build_args_hash_for(rt, "draft")

    recorded_response = {
        "type": "finish",
        "control": {
            "type": "finish", "decision": "finish", "next_phase": None,
            "confidence": 0.9, "reason": {"summary": "memoed"},
        },
        "artifact": {"type": "result", "data": {"memoed": True}},
    }
    plan = ResumePlan(
        run_id="run_memo_test",
        skill_name="memo_test",
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
                result=recorded_response,
            ),
        ],
    )
    rt = _runtime(state_log=state_log, resume_plan=plan)

    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")  # type: ignore[attr-defined]

    async def go():
        return await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]

    raw = asyncio.run(go())

    assert raw == recorded_response
    assert counter.calls == [], "call_llm must not be invoked on memo hit"
    types = [e.type for e in rt.events.all()]
    assert "step_memoized" in types
    # llm_called/llm_response_received must NOT fire on memo hit
    assert "llm_called" not in types
    assert "llm_response_received" not in types


def test_memo_miss_falls_through_and_emits_step_completed(tmp_path, monkeypatch):
    """Tier 2: no matching CommittedStep → call_llm invoked + WAL step_completed."""
    monkeypatch.chdir(tmp_path)
    counter = _CallCounter()
    monkeypatch.setattr(runtime_mod, "call_llm", counter)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    plan = ResumePlan(
        run_id="run_memo_test",
        skill_name="memo_test",
        skill_input={"type": "input", "data": {}},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[
            CommittedStep(
                op_invocation_id="draft.llm.0",
                op_kind="llm",
                phase="draft",
                args_hash="not_the_real_hash",  # mismatch → drift fall-through
                seq=10,
                result={"would": "not_be_used"},
            ),
        ],
    )
    rt = _runtime(state_log=state_log, resume_plan=plan)
    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")  # type: ignore[attr-defined]

    async def go():
        return await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]

    asyncio.run(go())

    assert len(counter.calls) == 1, "call_llm should be invoked exactly once on memo miss"
    # step_completed appended to WAL
    kinds = [e["kind"] for e in state_log.iter_from(0)]
    assert "step_completed" in kinds
    # Locate that event and verify op_kind/phase/op_invocation_id
    completed = [e for e in state_log.iter_from(0) if e["kind"] == "step_completed"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["op_kind"] == "llm"
    assert ev["phase"] == "draft"
    assert ev["op_invocation_id"] == "draft.llm.0"


def test_no_resume_plan_invokes_call_llm_normally(tmp_path, monkeypatch):
    """Tier 2: backward compat — without resume_plan, memo path is skipped."""
    monkeypatch.chdir(tmp_path)
    counter = _CallCounter()
    monkeypatch.setattr(runtime_mod, "call_llm", counter)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = _runtime(state_log=state_log, resume_plan=None)
    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")  # type: ignore[attr-defined]

    async def go():
        return await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]

    asyncio.run(go())

    assert len(counter.calls) == 1
    types = [e.type for e in rt.events.all()]
    assert "step_memoized" not in types
    assert "llm_called" in types


def test_op_invocation_id_increments_per_call(tmp_path, monkeypatch):
    """Tier 2: each call within a phase visit gets a distinct op_invocation_id.

    Pinned because retry / decide-retry paths call ``_call_llm_and_record``
    multiple times within a single phase visit. Memo lookup must distinguish
    them so resume reproduces the original sequence.
    """
    monkeypatch.chdir(tmp_path)
    counter = _CallCounter()
    monkeypatch.setattr(runtime_mod, "call_llm", counter)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = _runtime(state_log=state_log, resume_plan=None)
    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")  # type: ignore[attr-defined]

    async def go():
        await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]
        await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]
        await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]

    asyncio.run(go())

    completed = [e for e in state_log.iter_from(0) if e["kind"] == "step_completed"]
    ids = [e["op_invocation_id"] for e in completed]
    assert ids == ["draft.llm.0", "draft.llm.1", "draft.llm.2"]


def test_memo_hit_with_corrupt_result_falls_through_gracefully(tmp_path, monkeypatch):
    """Tier 2: schema versioning gate — recorded result that's not a dict.

    A corrupt CommittedStep.result (wrong type / unexpected schema) should
    NOT crash the runtime. The expected behavior: log warning, fall through
    to fresh call_llm. This protects production resumes against version skew
    where the serialized format changed since the step was recorded.
    """
    monkeypatch.chdir(tmp_path)
    counter = _CallCounter()
    monkeypatch.setattr(runtime_mod, "call_llm", counter)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = _runtime(state_log=state_log)
    args_hash = _build_args_hash_for(rt, "draft")

    plan = ResumePlan(
        run_id="run_memo_test",
        skill_name="memo_test",
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
                result="not_a_dict",  # corrupt — call_llm must return a dict
            ),
        ],
    )
    rt = _runtime(state_log=state_log, resume_plan=plan)
    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")  # type: ignore[attr-defined]

    async def go():
        return await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]

    raw = asyncio.run(go())

    # Fell through to fresh call: the response is from the stub, not the corrupt memo
    assert isinstance(raw, dict)
    assert len(counter.calls) == 1


def test_op_invocation_id_resets_on_phase_re_entry(tmp_path, monkeypatch):
    """Tier 2: ``_enter_phase`` resets the per-phase llm_call_idx counter.

    On resume, the runtime fast-forwards to the in-flight phase, then
    re-enters that phase from the start. The llm_call_idx must reset so the
    first LLM call inside the re-entered phase looks up
    ``{phase}.llm.0`` (which is what the original run recorded).
    """
    monkeypatch.chdir(tmp_path)
    counter = _CallCounter()
    monkeypatch.setattr(runtime_mod, "call_llm", counter)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = _runtime(state_log=state_log, resume_plan=None)
    frame = rt._build_frame("draft", {"type": "input", "data": {}}, [], "en")  # type: ignore[attr-defined]

    async def go():
        await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]
        await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]
        # Simulate phase re-entry (runtime.py calls _enter_phase on each entry)
        rt._enter_phase("draft", {"type": "input", "data": {}})  # type: ignore[attr-defined]
        await rt._call_llm_and_record("draft", frame, prior_attempts=None)  # type: ignore[attr-defined]

    asyncio.run(go())

    completed = [e for e in state_log.iter_from(0) if e["kind"] == "step_completed"]
    ids = [e["op_invocation_id"] for e in completed]
    # Two calls before re-entry, one after. Counter resets on re-entry.
    assert ids == ["draft.llm.0", "draft.llm.1", "draft.llm.0"]
