"""Tier 2: #1092 PR-C-4a — the CONVERGED op-loop invokes phase-axis compaction.

Recovers the coverage the deleted #1212 compaction_1212 test held for the retired
frame-fed op-loop. The json-mode ``_run_act_loop`` automatically summarises OLDER
``control_ir_results`` (via the SHARED ``compact_control_ir_results`` /
CompactionEngine) once they exceed ``recent_act_turns_raw``, keeping the decide
frame bounded and emitting ``phase_act_results_compacted``. The converged op-loop
did NOT do this (only an ``act_turn_reasoning`` tail-trim) — a json-mode-parity gap
that C-3's deletion of compaction_1212 deferred. PR-C-4a folds the converged path
into the SAME automatic compaction, in the phase layer (RouterLoop untouched →
chat byte-identical).

This pins it end-to-end through the real OSRuntime: across enough converged
act-turns that the accumulated ``control_ir_results`` exceeds
``recent_act_turns_raw``, the post-loop compaction fires before the FD2 decide
frame — the older results become a ``__compacted_phase_results__`` summary and
``phase_act_results_compacted`` is emitted.

Falsification: reverting the C-4a compaction block in ``_run_routerloop_op_loop``
makes the converged run emit NO ``phase_act_results_compacted`` (the converged path
pre-C-4a does not compact) → this test FAILS, proving it gates on the C-4a fold.

Real OSRuntime + real CompactionEngine + real control_ir_executor; the only
scripted seams are the module-level provider boundaries — ``call_llm`` /
``call_llm_tools`` (op-loop) and ``litellm.acompletion`` (the compaction summary).
"""
from __future__ import annotations

import asyncio
import json

import litellm

import reyn.kernel.llm_call_recorder as lcr
from reyn.config import CompactionConfig, PhaseActResultsCompactionConfig
from reyn.events.events import EventLog
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.services.compaction.engine import CompactionEngine

_COMPACTED_KIND = "__compacted_phase_results__"
_SKILL_NAME = "converged_compaction"

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
        allowed_ops=["read_file"],
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


class _TwoOpsThenStop:
    """turn 1 + 2 each emit a read_file op; turn 3 stops. After 2 ops the converged
    control_ir_results (len 2) exceeds recent_act_turns_raw=1, so the C-4a post-loop
    compaction fires before the FD2 decide."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        self.calls += 1
        if self.calls <= 2:
            return _tool_result([{
                "id": f"c{self.calls}", "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "notes.txt"}),
                },
            }])
        return _tool_result([])


def _finish_llm():
    async def _f(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))
    return _f


class _SummaryMsg:
    content = "COMPACTED older converged op results into one summary."


class _SummaryChoice:
    message = _SummaryMsg()
    finish_reason = "stop"


class _SummaryResp:
    choices = [_SummaryChoice()]
    usage = None


def test_converged_op_loop_invokes_phase_compaction(tmp_path, monkeypatch) -> None:
    """Tier 2: across converged act-turns where control_ir_results exceeds
    recent_act_turns_raw, the converged op-loop fires compact_control_ir_results
    (C-4a) and the older results become a __compacted_phase_results__ summary —
    recovering the deleted compaction_1212 coverage on the converged path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("fixture content for the converged compaction test")

    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _TwoOpsThenStop())

    async def _fake_acompletion(model, messages, **kw):  # noqa: ANN001, ANN003
        return _SummaryResp()
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    engine = CompactionEngine(
        model="gpt-3.5-turbo", events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True), T_SP=0,
    )
    # recent_act_turns_raw=1 → compact when >1 accumulated; threshold=1 token → the
    # older slice always exceeds it so the summary LLM call actually fires.
    pcfg = PhaseActResultsCompactionConfig(
        use_chars4_estimate=True,
        recent_act_turns_raw=1,
        summarize_older_threshold_tokens=1,
    )

    rt = OSRuntime(
        _skill(), model="stub/model", run_id="converged_compaction",
        tool_calls_op_loop_skills=[_SKILL_NAME],
        phase_compaction_engine=engine,
        phase_compaction_cfg=pcfg,
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert result is not None
    types = [e.type for e in rt.events.all()]
    # The converged path ran (not json-mode) AND fired phase-axis compaction.
    assert "phase_routerloop_op_loop_started" in types, (
        f"the converged op-loop must have run; events={types}"
    )
    assert "phase_act_results_compacted" in types, (
        "the converged op-loop must invoke phase-axis compaction once "
        f"control_ir_results exceeds recent_act_turns_raw (C-4a); events={types}"
    )
    # The compaction event payload confirms older results were folded into the
    # summary placeholder (public audit surface — the event carries the count +
    # the compacted kind), so the decide frame is bounded, not just flagged.
    compacted = [e for e in rt.events.all() if e.type == "phase_act_results_compacted"]
    assert compacted and compacted[0].data.get("n_older_compacted", 0) >= 1, (
        "phase_act_results_compacted must report >=1 older result folded into the "
        f"{_COMPACTED_KIND} summary; got {[e.data for e in compacted]}"
    )
