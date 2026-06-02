"""Tier 2: #1212 PR2 — native-tools op-loop gate dispatch + op execution.

The OS decides the phase mechanism (P3): a skill listed in
``tool_calls_op_loop_skills`` runs the native-tools op-loop (``_run_op_loop``), where
a phase's ops are emitted as native ``tool_calls`` and run through the SHARED
control_ir_executor; the decide turn is a separate json-mode transition call. A skill
NOT listed stays on the json-mode act loop (``_run_act_loop``), byte-for-byte
unchanged (the gate defaults to off).

Drives the real ``OSRuntime`` through its public ``run`` surface. The only scripted
seam is the module-level ``call_llm`` / ``call_llm_tools`` in ``llm_call_recorder``
(the provider boundary — the same sanctioned pattern as
``test_llm_call_recorder_invariants``), not a collaborator mock. Real ``EventLog``,
real ``control_ir_executor``, real workspace, real ``Skill``.
"""
from __future__ import annotations

import asyncio

import reyn.kernel.llm_call_recorder as lcr
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph

_FINISH_RESULT = {
    "type": "finish",
    "control": {
        "type": "finish", "decision": "finish", "next_phase": None,
        "confidence": 1.0, "reason": {"summary": "done"},
    },
    "artifact": {"type": "result", "data": {}},
}


def _skill(allowed_ops: list[str]) -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=allowed_ops,
    )
    return Skill(
        name="op_loop_gate", entry_phase="draft", phases={"draft": draft},
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


def _finish_call_llm():
    async def _f(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(
            data=_FINISH_RESULT,
            usage=TokenUsage(prompt_tokens=20, completion_tokens=10),
        )
    return _f


def test_gate_off_uses_json_mode(tmp_path, monkeypatch) -> None:
    """Tier 2: a skill NOT in tool_calls_op_loop_skills runs json-mode; the
    native-tools path (call_llm_tools) is never invoked."""
    monkeypatch.chdir(tmp_path)
    tools_calls: list[int] = []

    async def _tools(*a, **k):  # noqa: ANN002, ANN003
        tools_calls.append(1)
        return _tool_result([])

    monkeypatch.setattr(lcr, "call_llm", _finish_call_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _tools)

    rt = OSRuntime(_skill([]), model="stub/model", run_id="gate_off")
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert result is not None
    assert tools_calls == [], "opted-out skill must not invoke the native-tools path"
    assert any(e.type == "llm_called" for e in rt.events.all()), "json-mode call ran"


def test_gate_on_routes_to_op_loop(tmp_path, monkeypatch) -> None:
    """Tier 2: a skill in tool_calls_op_loop_skills runs the op-loop — the op-turn
    goes through call_llm_tools and (model emits no ops) the decide is a json-mode
    call. Proves the gate routes to _run_op_loop and its decide path."""
    monkeypatch.chdir(tmp_path)
    tools_calls: list[int] = []
    decide_calls: list[int] = []

    async def _tools(*a, **k):  # noqa: ANN002, ANN003
        tools_calls.append(1)
        return _tool_result([])  # no ops → op-loop goes straight to decide

    async def _decide(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        decide_calls.append(1)
        return LLMCallResult(
            data=_FINISH_RESULT,
            usage=TokenUsage(prompt_tokens=20, completion_tokens=10),
        )

    monkeypatch.setattr(lcr, "call_llm", _decide)
    monkeypatch.setattr(lcr, "call_llm_tools", _tools)

    rt = OSRuntime(
        _skill([]), model="stub/model", run_id="gate_on",
        tool_calls_op_loop_skills=["op_loop_gate"],
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert result is not None
    assert tools_calls == [1], "op-loop must invoke call_llm_tools for the op-turn"
    assert decide_calls == [1], "op-loop decide must use the json-mode call"


def test_gate_on_executes_op_via_tool_call(tmp_path, monkeypatch) -> None:
    """Tier 2: a native tool_call is converted to a ControlIROp and run through the
    SHARED control_ir_executor (act_executed event with the op kind), then the model
    stops emitting tool_calls → the json-mode decide finishes. Asserts the mechanism
    (tool_call → op → executor → event), independent of the op's own success."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("hi")

    turns = iter([
        _tool_result([{
            "id": "c1", "type": "function",
            "function": {"name": "file", "arguments": '{"op": "read", "path": "hello.txt"}'},
        }]),
        _tool_result([]),  # done with ops → decide
    ])

    async def _tools(*a, **k):  # noqa: ANN002, ANN003
        return next(turns)

    monkeypatch.setattr(lcr, "call_llm", _finish_call_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _tools)

    rt = OSRuntime(
        _skill(["file"]), model="stub/model", run_id="gate_op_exec",
        tool_calls_op_loop_skills=["op_loop_gate"],
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert result is not None
    acts = [e for e in rt.events.all() if e.type == "act_executed"]
    assert acts, "the tool_call must drive an act_executed (op ran via the shared executor)"
    assert "file" in acts[0].data["op_kinds"], "the converted op kind reaches the executor"
