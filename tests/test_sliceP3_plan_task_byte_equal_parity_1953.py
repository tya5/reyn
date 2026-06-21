"""Tier 3a: #1953 slice P3 — plan-path vs task-path byte-equal engine parity.

The deterministic half of the slice-P delete gate (Q3a). On ONE fixed decomposition
(goal + steps + deps), both execution engines — ``execute_plan`` (the path P4
deletes) and ``run_task_graph`` (the Task-driven successor) — are driven with the
**same scripted LLM responses** (one monkeypatched ``call_llm_tools`` keyed on the
step text, shared by both engines' RouterLoops). With identical LLM behavior the two
engines must produce **byte-equal** per-unit text + byte-equal synthesized reply —
the behavioral analog of P1's byte-identical MOVE. Any drift (SP construction,
narrowing, result-channel, aggregation order) fails this test, so it gates the
irreversible delete by construction rather than by coincidence.
"""
from __future__ import annotations

import pytest

from reyn.runtime.planner import Plan, PlanStep, execute_plan
from reyn.runtime.task_graph import build_task_graph, make_production_run_unit, run_task_graph
from reyn.task import InMemoryTaskBackend
from tests._support.router_loop import FakeRouterHost, text_result


@pytest.mark.asyncio
async def test_dependent_unit_sp_carries_goal_and_prior_results(monkeypatch):
    """Tier 2: regression for the SP-injection gap (b) live-parity caught — a
    dependent unit's step system prompt must carry the goal framing AND its deps'
    results (the I-2 result-channel rendered into the LLM context). (a) byte-equal
    could not catch this: the scripted LLM keyed on the user message and ignored
    the SP; only a real LLM reads the SP, so the channel was silently dropped."""
    captured: dict[str, str] = {}

    async def fake(**kwargs):
        msgs = kwargs.get("messages", [])
        sys_msg = next((str(m.get("content", "")) for m in msgs
                        if m.get("role") == "system"), "")
        users = [m for m in msgs if m.get("role") == "user"]
        seed = str(users[-1].get("content", "")) if users else ""
        captured[seed[:10]] = sys_msg
        return text_result(f"REPLY[{seed[:10]}]")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake)
    b = InMemoryTaskBackend()
    parent_id = await build_task_graph(
        b, goal="THE-DECOMP-GOAL", assignee="a2a:s", requester="r", steps=[
            {"id": "s1", "description": "alpha unit", "tools": [], "depends_on": []},
            {"id": "s2", "description": "beta unit", "tools": [], "depends_on": ["s1"]},
        ])
    run_unit = make_production_run_unit(
        FakeRouterHost(), chain_id="c", router_model=None, budget=None,
        goal="THE-DECOMP-GOAL")
    await run_task_graph(b, parent_id, run_unit=run_unit)

    s2_sp = captured["beta unit"[:10]]
    assert "THE-DECOMP-GOAL" in s2_sp                 # goal framing
    assert "Prior step results" in s2_sp              # the channel section
    assert "REPLY[alpha unit]" in s2_sp               # s1's actual result threaded in
    # s1 (no deps) gets the goal but no prior-results section.
    s1_sp = captured["alpha unit"[:10]]
    assert "THE-DECOMP-GOAL" in s1_sp
    assert "Prior step results" not in s1_sp

# A fixed decomposition with a diamond dependency (s4 depends on two branches) so
# the topological order + result-channel are exercised, not just a linear chain.
_GOAL = "summarize the module"
_STEPS = [
    {"id": "s1", "description": "read alpha file", "tools": [], "depends_on": []},
    {"id": "s2", "description": "read beta file", "tools": [], "depends_on": []},
    {"id": "s3", "description": "diff alpha vs beta", "tools": [], "depends_on": ["s1"]},
    {"id": "s4", "description": "write the final summary", "tools": [],
     "depends_on": ["s2", "s3"]},
]

# Scripted per-step replies, keyed on the (distinct, non-overlapping) descriptions.
_REPLIES = {
    "read alpha file": "ALPHA: contains foo()",
    "read beta file": "BETA: contains bar()",
    "diff alpha vs beta": "DIFF: foo vs bar",
    "write the final summary": "SUMMARY: module exposes foo() and bar()",
}


def _scripted_call_llm_tools():
    """A real callable replacing call_llm_tools for BOTH engines: returns the
    canned reply for whichever step description appears in the messages blob.
    Both engines feed the same step description as the seed, so each engine gets
    the identical reply for the identical step → byte-equal capture."""
    async def fake(**kwargs):
        blob = " ".join(str(m.get("content", "")) for m in kwargs.get("messages", []))
        for desc, reply in _REPLIES.items():
            if desc in blob:
                return text_result(reply)
        return text_result("__UNMATCHED__")
    return fake


@pytest.mark.asyncio
async def test_plan_and_task_engines_are_byte_equal(monkeypatch):
    """Tier 3a: identical scripted LLM → identical per-unit text + synthesized reply
    across the plan engine and the task engine."""
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _scripted_call_llm_tools())

    # --- plan path ---
    plan = Plan(goal=_GOAL, steps=tuple(
        PlanStep(id=s["id"], description=s["description"],
                 tools=tuple(s["tools"]), depends_on=tuple(s["depends_on"]))
        for s in _STEPS))
    plan_host = FakeRouterHost()
    plan_result = await execute_plan(
        plan, parent_host=plan_host, chain_id="c", budget=None, router_model=None)

    # --- task path ---
    b = InMemoryTaskBackend()
    parent_id = await build_task_graph(
        b, goal=_GOAL, assignee="a2a:s", requester="req", steps=_STEPS)
    task_host = FakeRouterHost()
    run_unit = make_production_run_unit(
        task_host, chain_id="c", router_model=None, budget=None, goal=_GOAL)
    task_final = await run_task_graph(b, parent_id, run_unit=run_unit)

    # 1. byte-equal synthesized reply (the user-facing aggregate).
    assert plan_result.text == task_final
    assert task_final == _REPLIES["write the final summary"]  # the topo-last sink

    # 2. byte-equal per-unit text — map plan's step_results (by step id) to the
    #    Task children (matched by description, since build_task_graph uuid-keys).
    children = {c.description: c for c in await b.list(parent_id=parent_id)}
    for s in _STEPS:
        plan_text = plan_result.step_results[s["id"]]
        task_text = children[s["description"]].result
        assert plan_text == task_text, f"step {s['id']!r} diverged: {plan_text!r} != {task_text!r}"
        assert plan_text == _REPLIES[s["description"]]
