"""Tier 2: #1092 PR-B enablement — the converged op-loop gate threads
config → Agent → OSRuntime → PhaseExecutor (the real run path).

Commit 2b wired ``routerloop_convergence_enabled`` at the OSRuntime↔PhaseExecutor
seam, but a direct-kwarg test would reproduce the #1248 advertise/wire-path trap:
it would pass with the production config→runtime PRODUCER missing. This pins the
full path: a skill named in ``config.routerloop_convergence_skills``, run via
``Agent.from_config(config)`` (the hub for swe_bench / run / cron / web / mcp /
eval), actually reaches ``PhaseExecutor._run_routerloop_op_loop`` (asserted via the
``phase_routerloop_op_loop_started`` event, the distinguishing marker vs the #1212
phase-native ``_run_op_loop``). A skill NOT in the list never reaches it.

Real ``Agent`` + real ``OSRuntime`` via the public ``Agent.from_config`` +
``agent.run``; the only scripted seam is the module-level ``call_llm`` /
``call_llm_tools`` provider boundary (the sanctioned pattern), not a collaborator
mock.
"""
from __future__ import annotations

import asyncio

import reyn.kernel.llm_call_recorder as lcr
from reyn.agent import Agent
from reyn.config import ReynConfig
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph

_SKILL_NAME = "converge_thread"

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


def _patch_llms(monkeypatch, tools_calls: list, decide_calls: list) -> None:
    async def _tools(*a, **k):  # noqa: ANN002, ANN003
        tools_calls.append(1)
        # No tool_calls → the op-loop reaches end_turn immediately; the converged
        # path then post-pends the FD2 json decide (separate ``call``).
        return LLMToolCallResult(
            content=None, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )

    async def _decide(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        decide_calls.append(1)
        return LLMCallResult(
            data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10),
        )

    monkeypatch.setattr(lcr, "call_llm", _decide)
    monkeypatch.setattr(lcr, "call_llm_tools", _tools)


def _event_kinds(subscribers_sink: list) -> list[str]:
    out: list[str] = []
    for ev in subscribers_sink:
        kind = getattr(ev, "type", None) or getattr(ev, "kind", None)
        if kind is None and isinstance(ev, dict):
            kind = ev.get("type") or ev.get("kind")
        if kind is not None:
            out.append(kind)
    return out


def test_from_config_gate_reaches_converged_op_loop(tmp_path, monkeypatch) -> None:
    """Tier 2: a skill in config.routerloop_convergence_skills, run via
    Agent.from_config, reaches the CONVERGED op-loop (RouterLoop.run_loop)."""
    monkeypatch.chdir(tmp_path)
    tools_calls: list[int] = []
    decide_calls: list[int] = []
    _patch_llms(monkeypatch, tools_calls, decide_calls)
    sink: list = []

    config = ReynConfig(routerloop_convergence_skills=[_SKILL_NAME])
    agent = Agent.from_config(
        config, shell_allowed=False, model="stub/model", subscribers=[sink.append],
    )
    result = asyncio.run(agent.run(_skill(), {"type": "input", "data": {}}))

    assert result.ok, f"run must complete; got {result.status}"
    assert "phase_routerloop_op_loop_started" in _event_kinds(sink), (
        "config.routerloop_convergence_skills must thread Agent.from_config → "
        "OSRuntime → PhaseExecutor._run_routerloop_op_loop (the converged path), "
        "not stay on the #1212 phase-native path"
    )
    # The op-loop ran (call_llm_tools) and the FD2 transition was a separate json
    # decide (call_llm) — P1/P8 post-pend after run_loop.
    assert tools_calls == [1], "the converged op-loop must invoke call_llm_tools"
    assert decide_calls == [1], "FD2: the transition decide uses the json-mode call"


def test_from_config_unlisted_skill_no_convergence(tmp_path, monkeypatch) -> None:
    """Tier 2: a skill NOT in the config list never reaches the converged op-loop
    (the start event is absent) — zero-change for un-opted skills."""
    monkeypatch.chdir(tmp_path)
    tools_calls: list[int] = []
    decide_calls: list[int] = []
    _patch_llms(monkeypatch, tools_calls, decide_calls)
    sink: list = []

    config = ReynConfig(routerloop_convergence_skills=[])  # nothing opted in
    agent = Agent.from_config(
        config, shell_allowed=False, model="stub/model", subscribers=[sink.append],
    )
    result = asyncio.run(agent.run(_skill(), {"type": "input", "data": {}}))

    assert result.ok, f"run must complete; got {result.status}"
    assert "phase_routerloop_op_loop_started" not in _event_kinds(sink), (
        "an unlisted skill must not reach the converged op-loop"
    )


def test_converged_op_loop_dispatches_phase_op_not_unknown_tool(tmp_path, monkeypatch) -> None:
    """Tier 2: a phase op tool_call (read_file) emitted in the converged op-loop
    DISPATCHES — the dispatch catalog is in sync with the advertised tools.

    Regression for the native-dispatch catalog gap (#1092 PR-B dogfood bar 1): the
    converged path advertises the phase's fine ops as ``tools=`` but the dispatch-side
    ``ctx.tool_catalog`` (= ``RouterLoop._catalog``) was only populated in ``run()``'s
    pre-loop, which the phase path bypasses (it drives ``run_loop`` directly) → a
    native ``read_file`` tool_call was advertised yet rejected as ``unknown_tool``.
    Fixed by syncing ``self._catalog`` from ``tools`` at ``run_loop`` start.
    """
    monkeypatch.chdir(tmp_path)
    sink: list = []
    turn = {"n": 0}

    async def _tools(*a, **k):  # noqa: ANN002, ANN003
        i = turn["n"]
        turn["n"] += 1
        if i == 0:
            return LLMToolCallResult(
                content=None,
                tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "x.txt"}'},
                }],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            )
        return LLMToolCallResult(
            content=None, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )

    async def _decide(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(
            data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10),
        )

    monkeypatch.setattr(lcr, "call_llm", _decide)
    monkeypatch.setattr(lcr, "call_llm_tools", _tools)

    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["read_file"],  # the op catalog advertises read_file as a tool
    )
    skill = Skill(
        name=_SKILL_NAME, entry_phase="draft", phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )
    config = ReynConfig(routerloop_convergence_skills=[_SKILL_NAME])
    agent = Agent.from_config(
        config, shell_allowed=False, model="stub/model", subscribers=[sink.append],
    )
    result = asyncio.run(agent.run(skill, {"type": "input", "data": {}}))

    assert result.ok, f"run must complete; got {result.status}"
    assert "phase_routerloop_op_loop_started" in _event_kinds(sink)
    # The advertised read_file tool_call must DISPATCH (catalog check passes) — never
    # be rejected as unknown_tool (the native-dispatch catalog gap). A nonexistent
    # path yields not_found, which is a successful dispatch (catalog OK), not
    # unknown_tool.
    assert "unknown_tool" not in repr(sink), (
        "a phase op tool_call must route through the converged path's ctx.tool_catalog, "
        "not be rejected as unknown_tool (the native-dispatch catalog gap)"
    )
    # #1092 PR-C-0 false-green fix: the op must DISPATCH SUCCESSFULLY through the FULL
    # converged path — RouterLoop._build_router_caller_state (called per dispatch) is
    # part of it. The earlier assertion only excluded ``unknown_tool``; a tool_failed
    # for ANOTHER reason (the eager ``list_available_skills`` AttributeError that died
    # before the gate, caught by sandbox_2 dogfood) slipped through. Pin the op
    # actually running: no tool_failed / AttributeError. (Falsified: reverting the
    # PR-C-0 host fix makes this FAIL.)
    assert "tool_failed" not in _event_kinds(sink), (
        "the phase op must dispatch through the FULL converged path (incl. "
        "_build_router_caller_state) without failing — a tool_failed here means the op "
        "died before the permission gate (e.g. the eager chat-discovery AttributeError)"
    )
    assert "AttributeError" not in repr(sink), (
        "no AttributeError from the converged dispatch path (RouterLoopCore host "
        "completeness / getattr-guard, #1092 PR-C-0)"
    )


def test_converged_decide_frame_information_equivalent_to_json_op_loop(tmp_path, monkeypatch) -> None:
    """Tier 2: the converged FD2 decide frame carries the SAME information the #1212
    json-mode op-loop decide frame does — act_turn_reasoning + RAW control_ir_results.

    Regression guard for the #1092 PR-B GATE-2 finding (the decide-fumble reliability
    regression, invisible to unit tests until dogfood): the converged decide frame was
    WEAKER than _run_op_loop's — (a) act_turn_reasoning was not collected from the native
    assistant turns, and (b) control_ir_results were the dispatch-wrapped
    ``{"status":"ok","data":<r>}`` envelope instead of the raw op result. Both = a general
    context-inadequacy bug. This pins information-equivalence so a future refactor can't
    silently re-introduce it.
    """
    monkeypatch.chdir(tmp_path)
    captured: dict = {"frame": None}
    turn = {"n": 0}

    async def _tools(*a, **k):  # noqa: ANN002, ANN003
        i = turn["n"]
        turn["n"] += 1
        if i == 0:
            return LLMToolCallResult(
                content="reading the file to decide",  # native assistant reasoning
                tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "x.txt"}'},
                }],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            )
        return LLMToolCallResult(
            content=None, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )

    async def _decide(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        captured["frame"] = frame
        return LLMCallResult(
            data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10),
        )

    monkeypatch.setattr(lcr, "call_llm", _decide)
    monkeypatch.setattr(lcr, "call_llm_tools", _tools)

    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["read_file"],
    )
    skill = Skill(
        name=_SKILL_NAME, entry_phase="draft", phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )
    config = ReynConfig(routerloop_convergence_skills=[_SKILL_NAME])
    agent = Agent.from_config(config, shell_allowed=False, model="stub/model")
    result = asyncio.run(agent.run(skill, {"type": "input", "data": {}}))

    assert result.ok, f"run must complete; got {result.status}"
    fr = captured["frame"]
    assert fr is not None, "the FD2 decide frame must be built + passed to the json decide call"
    # (a) reasoning continuity: the model's native assistant content is carried into the
    # decide frame (exactly what _run_op_loop carries via act_turn_reasoning).
    assert any("reading the file" in r for r in fr.act_turn_reasoning), (
        "converged decide frame must carry act_turn_reasoning from the native assistant "
        f"turns (got {fr.act_turn_reasoning!r})"
    )
    # (b) raw op-result shape: control_ir_results must be unwrapped, NOT the dispatch_tool
    # {"status":"ok","data":<r>} envelope (which the json-mode op-loop never carries).
    assert fr.control_ir_results, "the decide frame must carry the op results"
    for r in fr.control_ir_results:
        assert not (
            isinstance(r, dict) and "data" in r and set(r) <= {"status", "data", "error"}
        ), f"control_ir_results must be raw op results, not the dispatch envelope: {r!r}"
