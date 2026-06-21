"""Tier 2: #1953 slice P2 — per-Task cost attribution (slice-8 (B) completed).

The cost co-land: a Task's exec sub-loop carries its ``task_id``, injected at
RouterLoop construction (never a global handle), so every LLM call it makes is
attributed to that Task in the budget ledger; the exec driver then charges the
*recorded* cost delta onto the Task's cap counter through the slice-8
``record_task_cost`` prod-caller. These tests cover the three links of that chain.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from reyn.config import CostConfig
from reyn.core.op_runtime import task as taskmod
from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.task_graph import (
    build_task_graph,
    make_production_run_unit,
    run_task_graph,
)
from reyn.task import InMemoryTaskBackend
from tests._support.router_loop import FakeRouterHost, text_result


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


def test_record_llm_attributes_tokens_and_cost_to_task():
    """Tier 2: record_llm(task_id=...) attributes the call's tokens + cost to the
    Task (mirroring the per-purpose bucket); task_cost_usd() reads it back."""
    budget = BudgetTracker(CostConfig())
    budget.record_llm(model="gpt-4o-mini", agent="a",
                      usage=TokenUsage(prompt_tokens=1000, completion_tokens=500),
                      task_id="task-1")
    snap = budget.snapshot()
    assert snap["task_tokens"]["task-1"] == 1500
    assert snap["task_cost_usd"]["task-1"] == budget.task_cost_usd("task-1")
    # an unrelated call (no task_id) does not leak into the per-task bucket.
    budget.record_llm(model="gpt-4o-mini", agent="a",
                      usage=TokenUsage(prompt_tokens=10, completion_tokens=10))
    assert budget.snapshot()["task_tokens"]["task-1"] == 1500


@pytest.mark.asyncio
async def test_routerloop_injects_task_id_into_llm_call():
    """Tier 2: a RouterLoop constructed with task_id forwards it to the LLM call
    (the construction-injection); a loop without one omits the kwarg, so the
    existing test-fake / production signatures stay intact."""
    captured: list[Any] = []

    async def fake_llm(**kwargs):
        captured.append(kwargs.get("task_id", "__absent__"))
        return text_result("ok")

    host = FakeRouterHost()
    await RouterLoop(host=host, chain_id="c", task_id="task-9",
                    llm_caller=fake_llm).run(user_text="go", history=[])
    await RouterLoop(host=host, chain_id="c",
                    llm_caller=fake_llm).run(user_text="go", history=[])
    assert captured == ["task-9", "__absent__"]


@pytest.mark.asyncio
async def test_production_run_unit_charges_recorded_cost_onto_task(monkeypatch):
    """Tier 2: the full chain minus litellm — make_production_run_unit builds a
    task_id-carrying RouterLoop; the (faked) call_llm_tools records cost under that
    task_id; run_task_graph charges the recorded delta onto the Task via
    record_task_cost. cost_accum == the budget's recorded task cost (slice-8 (B))."""
    budget = BudgetTracker(CostConfig())

    async def fake_call_llm_tools(**kwargs):
        # Stand in for the real call_llm_tools' post-record (which now forwards
        # task_id). The RouterLoop interprets the returned text reply + captures it.
        budget.record_llm(model="gpt-4o-mini", agent=kwargs.get("budget_agent"),
                          usage=TokenUsage(prompt_tokens=2000, completion_tokens=1000),
                          task_id=kwargs.get("task_id"))
        return text_result("unit reply")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_call_llm_tools)

    b = InMemoryTaskBackend()
    parent_host = FakeRouterHost()
    parent_id = await build_task_graph(
        b, goal="g", assignee="a2a:s", requester="req",
        steps=[{"id": "s1", "description": "only", "tools": []}])
    child = (await b.list(parent_id=parent_id))[0]

    ctx = SimpleNamespace(session_id="req", agent_id="a", events=None,
                          task_backend=b, task_waker=None)

    async def on_unit_cost(task_id, cost):
        await taskmod.record_task_cost(ctx, task_id, cost)

    run_unit = make_production_run_unit(
        parent_host, chain_id="c", router_model=None, budget=budget)
    final = await run_task_graph(b, parent_id, run_unit=run_unit,
                                 on_unit_cost=on_unit_cost)

    # the unit's reply was captured + synthesized; its cost was attributed to the
    # Task in the ledger AND charged onto the Task's cap counter (the same number).
    assert final == "unit reply"
    assert budget.snapshot()["task_tokens"][child.task_id] == 3000
    charged = (await b.get(child.task_id)).cost_accum
    assert charged == budget.task_cost_usd(child.task_id)
    assert charged > 0.0
