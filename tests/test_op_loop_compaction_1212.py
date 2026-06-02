"""Tier 2: #1212 — the native-tools op-loop invokes phase-axis compaction.

Frame-fed (ADR-0035 D2-impl) is load-bearing *because* the op-loop accumulates op
results into ``control_ir_results`` and rebuilds the frame each turn — which means
the existing phase-axis compaction (shared with json-mode ``_run_act_loop``) keeps
the prompt bounded. PR2 added the op-loop's own copy of that compaction block but
neither the 1-op 動作確認 nor the gate/conversion tests fire it. This pins it: across
enough act-turns that ``control_ir_results`` exceeds ``recent_act_turns_raw``,
``_run_op_loop`` calls ``compact_control_ir_results`` and the older results are
replaced by a ``__compacted_phase_results__`` summary (regression guard that
frame-fed preserves compaction).

Real ``OSRuntime`` + real ``CompactionEngine`` + real ``control_ir_executor``; the
only scripted seams are the module-level provider boundaries — ``call_llm`` /
``call_llm_tools`` (op-loop, in ``llm_call_recorder``) and ``litellm.acompletion``
(the compaction summary, via ``recorded_acompletion``). No collaborator mocks.
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
        name="op_loop_compaction", entry_phase="draft", phases={"draft": draft},
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
    """turn 1 + 2 each emit a file read op; turn 3+ stops. After 2 ops the
    accumulated control_ir_results (len 2) exceeds recent_act_turns_raw=1, so the
    compaction block fires at the top of the third loop iteration."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        self.calls += 1
        if self.calls <= 2:
            return _tool_result([{
                "id": f"c{self.calls}", "type": "function",
                "function": {"name": "file", "arguments": json.dumps({"op": "read", "path": "notes.txt"})},
            }])
        return _tool_result([])


def _finish_llm():
    async def _f(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))
    return _f


class _SummaryMsg:
    content = "COMPACTED older op results into one summary."


class _SummaryChoice:
    message = _SummaryMsg()
    finish_reason = "stop"


class _SummaryResp:
    choices = [_SummaryChoice()]
    usage = None


def test_op_loop_invokes_phase_compaction(tmp_path, monkeypatch) -> None:
    """Tier 2: across act-turns where control_ir_results exceeds recent_act_turns_raw,
    _run_op_loop fires compact_control_ir_results and older results become a
    __compacted_phase_results__ summary."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("fixture content for the op-loop compaction test")

    # op-loop LLM seams (replaced — never reach litellm.acompletion)
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _TwoOpsThenStop())
    # compaction summary seam (the engine's recorded_acompletion → litellm.acompletion)
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
        _skill(), model="stub/model", run_id="op_loop_compaction",
        tool_calls_op_loop_skills=["op_loop_compaction"],
        phase_compaction_engine=engine,
        phase_compaction_cfg=pcfg,
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert result is not None
    types = [e.type for e in rt.events.all()]
    assert "phase_act_results_compacted" in types, (
        "the op-loop must invoke phase-axis compaction once control_ir_results "
        f"exceeds recent_act_turns_raw; events={types}"
    )
