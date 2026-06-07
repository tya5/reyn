"""Tier 2: #1092 PR-C-4b — the converged op-loop's IN-LOOP message-history is
proactively BOUNDED (not linear) by per-turn compaction.

The converged op-loop threads op results as native ``tool`` messages that
accumulate linearly (measured: +N tok/op, no proactive bound — RouterLoop's
retry-shrink / voluntary-compact are overflow-only last-resorts). json-mode's act
loop proactively compacts older results once they exceed ``recent_act_turns_raw``;
C-4b folds the converged path into the SAME bounding via a per-turn host hook
(``PhaseRouterLoopHost.maybe_compact_messages``) that summarises OLDER ``tool``
message contents through the SHARED ``compact_control_ir_results`` primitive
(no bespoke logic), in-place (no message added/removed → tool_call_id pairing +
role-alternation stay API-valid).

This pins the BEHAVIOR end-to-end: across many converged act-turns with sizeable
op results + a low compaction threshold, the serialised message-history token
count PLATEAUS (bounded) instead of growing linearly with the turn count, and
``phase_act_results_compacted`` is emitted.

Falsification: with the compaction engine/cfg unwired (the hook becomes a no-op),
the history grows LINEARLY (asserted by the sibling no-compaction control) — so the
bounding gates on the C-4b hook firing.

Crash-resume is out of scope here (json-mode-parity, tracked #1267): the no-compaction
window keeps op + LLM memo-HIT (#1263 / #1264); compaction×resume memo drift is a
shared pre-existing gap, not introduced by C-4b.

Real OSRuntime + real CompactionEngine; scripted seams are the module-level
``call_llm`` / ``call_llm_tools`` (op-loop) and ``litellm.acompletion`` (summary).
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
from reyn.services.compaction.engine import CompactionEngine, estimate_tokens

_SKILL_NAME = "converged_inloop_compaction"
_N_OPS = 8
# A sizeable op result so each accumulated tool message is non-trivial and the
# bounded-vs-linear difference is unambiguous.
_OP_RESULT_FILLER = "lorem ipsum dolor sit amet " * 40

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
        max_act_turns=_N_OPS + 2,
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


class _NReadsThenStop:
    """Emit a read_file op for _N_OPS turns, then stop. Captures the serialised
    message-history token estimate it RECEIVES each turn (the growth curve)."""

    def __init__(self) -> None:
        self.calls = 0
        self.tokens_per_turn: list[int] = []

    async def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        msgs = k.get("messages") or (a[1] if len(a) > 1 else [])
        self.tokens_per_turn.append(
            estimate_tokens(json.dumps(msgs, default=str, ensure_ascii=False), "gpt-3.5-turbo")
        )
        i = self.calls
        self.calls += 1
        if i < _N_OPS:
            return _tool_result([{
                "id": f"c{i}", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "notes.txt"})},
            }])
        # #1092↔#187 reconciliation: a clean op-loop end emits CONTENT (a final
        # assistant message). A content-LESS stop is now (correctly) treated as a
        # premature empty-stop glitch and nudged once by the #187 agent-path
        # resume retry — which would inject an extra "resume" turn and corrupt
        # this per-op growth measurement (the terminal delta would be the small
        # resume turn, not a full op-result). The content-less terminator was an
        # incidental simplification; the LOAD-BEARING assertion is the LINEAR
        # per-op delta (≈ full op-result size — the unbounded result accumulation
        # the C-4b hook closes), NOT the terminator's content-ness. A content-
        # bearing clean end preserves the measurement while avoiding the (correct)
        # empty-stop nudge.
        return LLMToolCallResult(
            content="done", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )


def _finish_llm():
    async def _f(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))
    return _f


class _SummaryMsg:
    content = "[short compacted summary of earlier reads]"


class _SummaryChoice:
    message = _SummaryMsg()
    finish_reason = "stop"


class _SummaryResp:
    choices = [_SummaryChoice()]
    usage = None


def _run(tmp_path, monkeypatch, *, wire_compaction: bool):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text(_OP_RESULT_FILLER)

    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    script = _NReadsThenStop()
    monkeypatch.setattr(lcr, "call_llm_tools", script)

    async def _fake_acompletion(model, messages, **kw):  # noqa: ANN001, ANN003
        return _SummaryResp()
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    kwargs = {}
    if wire_compaction:
        kwargs["phase_compaction_engine"] = CompactionEngine(
            model="gpt-3.5-turbo", events=EventLog(),
            cfg=CompactionConfig(use_chars4_estimate=True), T_SP=0,
        )
        kwargs["phase_compaction_cfg"] = PhaseActResultsCompactionConfig(
            use_chars4_estimate=True, recent_act_turns_raw=1,
            summarize_older_threshold_tokens=1,
        )

    rt = OSRuntime(
        _skill(), model="stub/model", run_id="convc4b",
        tool_calls_op_loop_skills=[_SKILL_NAME], **kwargs,
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))
    types = [e.type for e in rt.events.all()]
    return result, script.tokens_per_turn, types


def test_converged_inloop_message_history_bounded_by_compaction(tmp_path, monkeypatch) -> None:
    """Tier 2: with C-4b compaction wired, the converged op-loop's in-loop
    message-history PLATEAUS (bounded) instead of growing linearly, and
    phase_act_results_compacted is emitted."""
    result, tokens, types = _run(tmp_path, monkeypatch, wire_compaction=True)
    assert result is not None
    assert "phase_routerloop_op_loop_started" in types, f"converged path must run; {types[:8]}"
    assert "phase_act_results_compacted" in types, (
        f"C-4b per-turn compaction must fire across the op-loop; events={types}"
    )
    assert len(tokens) >= _N_OPS, f"expected ~{_N_OPS} act-turn samples; got {len(tokens)}"
    # BOUNDED (result-content): without compaction each read accumulates the FULL
    # op-result content (≈ op_result_tokens / op — the large-op degradation driver:
    # per-op growth scales with op-result size). With C-4b the OLDER op-result
    # contents collapse to one summary, so the steady-state per-op growth is the
    # FIXED tool_call-request overhead (id/name/args), which does NOT scale with
    # result size. Assert the steady per-op delta is WELL BELOW the op-result size
    # (= the result content is being summarised, not accumulated).
    op_result_tokens = estimate_tokens(_OP_RESULT_FILLER, "gpt-3.5-turbo")
    steady_delta = tokens[-1] - tokens[-2]
    assert steady_delta < op_result_tokens * 0.5, (
        "C-4b must bound the op-result-content growth: the steady per-op delta "
        f"({steady_delta}) must be well below the op-result size ({op_result_tokens}) "
        f"— i.e. older results summarised, not accumulated; series={tokens}"
    )


def test_falsification_unwired_compaction_grows_linearly(tmp_path, monkeypatch) -> None:
    """Tier 2: falsification control — with the compaction engine/cfg UNWIRED the
    hook is a no-op, so the converged in-loop history grows LINEARLY (per-op delta
    ≈ full op-result size), proving the sibling test's bounding gates on the C-4b
    hook firing."""
    result, tokens, types = _run(tmp_path, monkeypatch, wire_compaction=False)
    assert result is not None
    assert "phase_act_results_compacted" not in types, "no compaction without engine/cfg"
    assert len(tokens) >= _N_OPS
    # LINEAR: without C-4b each read accumulates the FULL op-result content, so the
    # steady per-op delta ≈ the op-result size (the unbounded, result-size-scaling
    # growth the hook closes — the measure_b.py baseline).
    op_result_tokens = estimate_tokens(_OP_RESULT_FILLER, "gpt-3.5-turbo")
    steady_delta = tokens[-1] - tokens[-2]
    assert steady_delta > op_result_tokens * 0.8, (
        "without C-4b the steady per-op delta must ≈ the full op-result size "
        f"({op_result_tokens}) — unbounded result accumulation; got {steady_delta}, "
        f"series={tokens}"
    )
