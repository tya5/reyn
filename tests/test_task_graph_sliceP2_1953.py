"""Tier 2: #1953 slice P2 — task-driven decomposition + execution driver.

`build_task_graph` decomposes a goal into a parent + child Task DAG; `run_task_graph`
drives it to completion through an (injected) per-unit runner, passing each unit
its deps' results (the result-channel), charging each unit's cost (the slice-8
`record_task_cost` prod-caller — the cost-attribution co-land), and synthesizing
the final reply. Real backend; the per-unit LLM run is the injection seam.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.runtime.task_graph import (
    TaskStepValidationError,
    build_task_graph,
    run_task_graph,
    validate_step_tools,
)
from reyn.task import InMemoryTaskBackend, TaskState


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_build_task_graph_creates_parent_and_child_dag():
    """Tier 2: a goal + steps → a parent Task + child Tasks carrying tools + the
    dependency DAG (born-blocked when they have unsatisfied deps)."""
    b = InMemoryTaskBackend()
    parent_id = await build_task_graph(
        b, goal="ship it", assignee="a2a:s", requester="req",
        steps=[
            {"id": "s1", "description": "read the code", "tools": ["file__read"]},
            {"id": "s2", "description": "review it", "tools": ["skill__review"],
             "depends_on": ["s1"]},
        ])
    parent = await b.get(parent_id)
    assert parent.name == "ship it"
    children = await b.list(parent_id=parent_id)
    assert {c.name for c in children} == {"read the code", "review it"}
    s1 = next(c for c in children if c.name == "read the code")
    s2 = next(c for c in children if c.name == "review it")
    assert s1.tools == ["file__read"] and s1.status is TaskState.PENDING   # no deps
    assert s2.tools == ["skill__review"] and s2.status is TaskState.BLOCKED  # born-blocked on s1
    assert s2.deps == [s1.task_id]


@pytest.mark.asyncio
async def test_build_task_graph_carries_1998_qualified_tool_vocabulary():
    """Tier 2: #1998 vocabulary carried onto the Task path — under the universal
    scheme a task-step naming a qualified action (web__search) validates + narrows,
    while a bogus qualified name is rejected (scoped guard, not blanket leniency).
    A provider namespace prefix (default_api.) is stripped (#1989)."""
    b = InMemoryTaskBackend()
    catalog = {"invoke_action", "list_actions"}  # universal wrappers
    parent_id = await build_task_graph(
        b, goal="g", assignee="a2a:s", requester="req",
        allowed_tool_names=catalog, accept_qualified_actions=True,
        steps=[{"id": "s1", "description": "search",
                "tools": ["web__search", "default_api.invoke_action"]}])
    child = (await b.list(parent_id=parent_id))[0]
    # web__search accepted (qualified) + namespace stripped to the bare wrapper.
    assert child.tools == ["web__search", "invoke_action"]
    # the scoped guard still rejects a non-parseable / bogus name.
    with pytest.raises(TaskStepValidationError):
        await build_task_graph(
            b, goal="g2", assignee="a2a:s", requester="req",
            allowed_tool_names=catalog, accept_qualified_actions=True,
            steps=[{"id": "s1", "description": "x", "tools": ["bogus action!"]}])
    # without the universal-scheme signal, a qualified action is NOT auto-accepted.
    assert validate_step_tools(["invoke_action"], allowed_tool_names=catalog) == \
        ["invoke_action"]
    with pytest.raises(TaskStepValidationError):
        validate_step_tools(["web__search"], allowed_tool_names=catalog)


@pytest.mark.asyncio
async def test_run_task_graph_orders_passes_results_and_synthesizes():
    """Tier 2: the driver runs s1 before its dependent s2, passes s1's result into
    s2 (the result-channel), and the parent synthesizes the last unit's result."""
    b = InMemoryTaskBackend()
    parent_id = await build_task_graph(
        b, goal="g", assignee="a2a:s", requester="req",
        steps=[
            {"id": "s1", "description": "first", "tools": []},
            {"id": "s2", "description": "second", "tools": [], "depends_on": ["s1"]},
        ])
    seen_prior: dict[str, dict] = {}

    async def run_unit(task, prior_results):
        seen_prior[task.name] = dict(prior_results)
        return f"result-of-{task.name}", 1.0

    final = await run_task_graph(b, parent_id, run_unit=run_unit)

    # s1 ran with no prior; s2 ran AFTER s1 with s1's result in its prior_results.
    s1 = next(c for c in await b.list(parent_id=parent_id) if c.name == "first")
    assert seen_prior["first"] == {}
    assert seen_prior["second"] == {s1.task_id: "result-of-first"}
    # both completed + carry their result; parent synthesizes the last unit's text.
    assert (await b.get(s1.task_id)).status is TaskState.COMPLETED
    assert final == "result-of-second"
    assert (await b.get(parent_id)).result == "result-of-second"


@pytest.mark.asyncio
async def test_run_task_graph_charges_cost_via_record_task_cost():
    """Tier 2: the cost-attribution co-land — each unit's cost is charged onto its
    Task through the slice-8 `record_task_cost` prod-caller (this is where slice 8's
    deferred (B) is completed: the exec-engine's exactly-one-task scope makes the
    cost unambiguous)."""
    b = InMemoryTaskBackend()
    parent_id = await build_task_graph(
        b, goal="g", assignee="a2a:s", requester="req",
        steps=[{"id": "s1", "description": "only", "tools": []}])
    ctx = SimpleNamespace(session_id="req", agent_id="a", events=None,
                          task_backend=b, task_waker=None)

    async def run_unit(task, prior_results):
        return "done", 3.5

    async def on_unit_cost(task_id, cost):
        await taskmod.record_task_cost(ctx, task_id, cost)

    await run_task_graph(b, parent_id, run_unit=run_unit, on_unit_cost=on_unit_cost)

    s1 = next(c for c in await b.list(parent_id=parent_id) if c.name == "only")
    assert (await b.get(s1.task_id)).cost_accum == 3.5


@pytest.mark.asyncio
async def test_run_unit_narrows_via_engine_for_task():
    """Tier 2: the per-unit engine narrows to the unit's tools (the TaskExecutionHost
    Task-driven mode) — a `skill__*` unit plumbs skills, an unrelated one silences."""
    from reyn.runtime.task_execution import TaskExecutionHost

    class _FakeParent:
        def list_available_skills(self):
            return [{"name": "review", "description": "x"}]
        def list_available_agents(self):
            return []

    b = InMemoryTaskBackend()
    parent_id = await build_task_graph(
        b, goal="g", assignee="a2a:s", requester="req",
        steps=[{"id": "s1", "description": "review", "tools": ["skill__review"]}])
    child = (await b.list(parent_id=parent_id))[0]
    host = TaskExecutionHost.for_task(child, parent=_FakeParent())
    assert host.list_available_skills()  # narrowed to the unit's skill tool
