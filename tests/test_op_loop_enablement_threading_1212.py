"""Tier 2: #1212 enablement — the op-loop gate threads config → Agent → OSRuntime.

PR2 wired the op-loop gate at the OSRuntime↔PhaseExecutor seam but no production
caller threaded `tool_calls_op_loop_skills` in — the op-loop was reachable only by
constructing OSRuntime directly in a test. This pins the real-path wiring: a skill
named in `config.tool_calls_op_loop_skills`, run via `Agent.from_config(config)`
(the hub for swe_bench / run / cron / web / mcp / eval), actually runs the
native-tools op-loop. A skill NOT in the list stays json-mode (unchanged).

Real `Agent` + real `OSRuntime` via the public `Agent.from_config` + `agent.run`;
the only scripted seam is the module-level `call_llm` / `call_llm_tools` provider
boundary (the sanctioned pattern), not a collaborator mock.
"""
from __future__ import annotations

import asyncio

import reyn.kernel.llm_call_recorder as lcr
from reyn.agent import Agent
from reyn.config import ReynConfig
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph

_SKILL_NAME = "op_loop_thread"

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
        allowed_ops=[],
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


def _patch_llms(monkeypatch, tools_calls: list, decide_calls: list) -> None:
    async def _tools(*a, **k):  # noqa: ANN002, ANN003
        tools_calls.append(1)
        return _tool_result([])  # no ops → straight to the json decide

    async def _decide(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        decide_calls.append(1)
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))

    monkeypatch.setattr(lcr, "call_llm", _decide)
    monkeypatch.setattr(lcr, "call_llm_tools", _tools)


def test_from_config_gate_threads_to_op_loop(tmp_path, monkeypatch) -> None:
    """Tier 2: a skill in config.tool_calls_op_loop_skills, run via Agent.from_config,
    runs the op-loop (call_llm_tools invoked)."""
    monkeypatch.chdir(tmp_path)
    tools_calls: list[int] = []
    decide_calls: list[int] = []
    _patch_llms(monkeypatch, tools_calls, decide_calls)

    config = ReynConfig(tool_calls_op_loop_skills=[_SKILL_NAME])
    agent = Agent.from_config(config, shell_allowed=False, model="stub/model")
    result = asyncio.run(agent.run(_skill(), {"type": "input", "data": {}}))

    assert result.ok, f"run must complete; got {result.status}"
    assert tools_calls == [1], (
        "config gate must thread Agent.from_config → OSRuntime → op-loop "
        "(call_llm_tools invoked for the op-turn)"
    )
    assert decide_calls == [1], "op-loop decide uses the json-mode call"


def test_from_config_unlisted_skill_stays_json_mode(tmp_path, monkeypatch) -> None:
    """Tier 2: a skill NOT in the config list runs json-mode — call_llm_tools is
    never invoked (zero-change for un-opted skills)."""
    monkeypatch.chdir(tmp_path)
    tools_calls: list[int] = []
    decide_calls: list[int] = []
    _patch_llms(monkeypatch, tools_calls, decide_calls)

    config = ReynConfig(tool_calls_op_loop_skills=[])  # empty → nothing opted in
    agent = Agent.from_config(config, shell_allowed=False, model="stub/model")
    result = asyncio.run(agent.run(_skill(), {"type": "input", "data": {}}))

    assert result.ok, f"run must complete; got {result.status}"
    assert tools_calls == [], "an unlisted skill must not invoke the native-tools path"
