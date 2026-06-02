"""Tier 2: #1212 — op-loop reasoning-continuity (decision A2).

A capable model may emit inline content (its reasoning) alongside a tool_call on an
op-loop act turn. PR2's frame-fed loop discarded that content — losing the model's
reasoning thread across turns. This carries it forward: `_run_op_loop` appends a
non-empty `result.content` to `ContextFrame.act_turn_reasoning` (bounded to the last
`recent_act_turns_raw` entries, no compaction LLM call), so the NEXT turn's frame
shows the model its own prior reasoning. (No-op for weak models like flash-lite that
emit `content=None` on tool_call turns.)

Real `OSRuntime` + real `ControlIRExecutor`; the only scripted seam is the
module-level `call_llm` / `call_llm_tools` provider boundary, which also captures the
messages each turn so the test can assert the carried reasoning reaches turn 2.
No collaborator mocks.
"""
from __future__ import annotations

import asyncio
import json

import reyn.kernel.llm_call_recorder as lcr
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph

_REASONING = "REASONING_MARKER_carry_me_forward"

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
        name="op_loop_reasoning", entry_phase="draft", phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _tool_result(tool_calls: list, content: str | None = None) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=content, tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
    )


def test_op_loop_carries_model_reasoning_forward(tmp_path, monkeypatch) -> None:
    """Tier 2: a non-empty content on act turn 1 is carried into act turn 2's frame
    (act_turn_reasoning), so the model sees its own prior reasoning."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("fixture")
    captured: list[list] = []

    class _Script:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, *, messages, **k):  # noqa: ANN002, ANN003
            self.calls += 1
            captured.append(messages)
            if self.calls == 1:
                # reasoning emitted alongside the file op
                return _tool_result(
                    [{
                        "id": "c1", "type": "function",
                        "function": {"name": "file", "arguments": '{"op": "read", "path": "notes.txt"}'},
                    }],
                    content=_REASONING,
                )
            return _tool_result([])  # turn 2: stop → decide

    async def _decide(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))

    monkeypatch.setattr(lcr, "call_llm", _decide)
    monkeypatch.setattr(lcr, "call_llm_tools", _Script())

    rt = OSRuntime(
        _skill(), model="stub/model", run_id="op_loop_reasoning",
        tool_calls_op_loop_skills=["op_loop_reasoning"],
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert result is not None
    assert captured, "the op-loop must have invoked call_tools"
    first_turn, *later_turns = captured
    # The first turn's prompt must NOT contain the reasoning (not yet produced).
    assert _REASONING not in json.dumps(first_turn), "turn 1 must not carry future reasoning"
    # A later turn's prompt MUST carry turn 1's reasoning forward (act_turn_reasoning).
    assert any(_REASONING in json.dumps(m) for m in later_turns), (
        "the model's act-turn-1 inline content must be carried into a later turn's "
        "frame (act_turn_reasoning) for reasoning continuity"
    )
