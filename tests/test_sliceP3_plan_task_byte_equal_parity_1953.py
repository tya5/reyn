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
        task_host, chain_id="c", router_model=None, budget=None)
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
