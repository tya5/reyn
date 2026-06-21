"""Task-driven decomposition + execution (#1953 slice P2).

The internal entry that subsumes ``plan``: decompose a goal into a parent Task +
a child Task DAG (each child = a unit of work with its own narrowed ``tools`` and
``depends_on`` edges), then drive the DAG to completion through the
``TaskExecutionHost`` engine, propagating readiness (slices 6/6-ext) as each unit
completes and synthesizing the final reply from the children's results.

P2 keeps this **internal** (not an LLM-facing router tool yet — the router-expose
co-lands with the plan-tool repoint at P3, avoiding a dual LLM-facing entry while
``plan`` still runs in parallel). The orchestration takes an injectable
``run_unit`` so it is testable without a live RouterLoop / LLM; the production
``run_unit`` builds the engine + sub-loop.

Result-channel (I-2): a unit's reply text lands on its Task (``set_result``); a
dependent reads its deps' results from the backend at run time. The edges are pure
topology — which dep's result feeds a unit is the unit's (the LLM's) concern, not a
graph property.
"""
from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from reyn.task import Task, TaskState
from reyn.tools.universal_catalog import (
    is_valid_qualified_name,
    strip_provider_tool_namespace,
)

# run_unit(task, prior_results) -> (result_text, cost_usd). The production impl
# builds a TaskExecutionHost + sub-loop and runs it; tests inject a stub.
RunUnit = Callable[[Any, "dict[str, str]"], Awaitable["tuple[str, float]"]]


class TaskStepValidationError(ValueError):
    """A task-step's ``tools`` reference a tool outside the available catalog."""


def validate_step_tools(
    tools: "list[Any]",
    *,
    allowed_tool_names: "set[str]",
    accept_qualified_actions: bool = False,
) -> "list[str]":
    """Normalize + validate a task-step's narrowing tool set, returning the
    normalized names. Carries the plan path's tool-vocabulary correctness onto
    the Task system (#1953 slice P2):

    - #1989: strip a provider function-calling namespace (``default_api.``) a weak
      model may echo onto a tool name, so the Task stores the bare catalog name.
    - #1998 / #2004: under the universal scheme the callable tools are the
      wrappers (``invoke_action``/…) but a step's narrowing vocabulary is the
      qualified ``<category>__action`` names (which the exec engine's family-gate
      keys on, and which ``invoke_action`` can reach). When the caller signals
      that scheme (``accept_qualified_actions``), accept any *valid* qualified
      action name in addition to the wrapper allow-set — scoped, not blanket
      leniency (reachability via ``invoke_action`` is unchanged)."""
    normalized: list[str] = []
    for t in tools:
        if not isinstance(t, str):
            raise TaskStepValidationError("task step tools[*] must be strings")
        nt = strip_provider_tool_namespace(t)
        if nt not in allowed_tool_names and not (
            accept_qualified_actions and is_valid_qualified_name(nt)
        ):
            allowed = sorted(allowed_tool_names)
            extra = (
                " (or a qualified <category>__action name)"
                if accept_qualified_actions else ""
            )
            raise TaskStepValidationError(
                f"task step tool {t!r} is not in the available tool catalog. "
                f"Allowed: {allowed}{extra}"
            )
        normalized.append(nt)
    return normalized


async def build_task_graph(
    backend: Any,
    *,
    goal: str,
    steps: "list[dict]",
    assignee: str,
    requester: str,
    created_by: "str | None" = None,
    allowed_tool_names: "set[str] | None" = None,
    accept_qualified_actions: bool = False,
) -> str:
    """Create the parent Task (the goal) + a child Task per step, wiring each
    child's ``depends_on`` (step ids) into the Task DAG. Returns the parent
    ``task_id``. The shared edge-guard cycle-checks every dep on create (a cycle
    raises ``TaskCycleError`` / a dangling dep ``TaskDepNotFoundError``).

    When ``allowed_tool_names`` is given each step's ``tools`` are validated +
    normalized through :func:`validate_step_tools` (the router entry passes its
    catalog + ``accept_qualified_actions=True`` under the universal scheme); when
    None the tools are stored as-is (the internal driver path, P2).

    ``steps`` items: ``{"id", "description", "tools", "depends_on"?}``."""
    parent = await backend.create(Task(
        task_id=uuid.uuid4().hex, name=goal, description=goal,
        assignee=assignee, requester=requester, created_by=created_by,
    ))
    # Pre-allocate child ids so a step's depends_on (step ids) maps to task ids.
    id_map: dict[str, str] = {s["id"]: uuid.uuid4().hex for s in steps}
    for s in steps:
        deps = [id_map[d] for d in s.get("depends_on", [])]
        raw_tools = list(s.get("tools", []))
        tools = (
            validate_step_tools(
                raw_tools, allowed_tool_names=allowed_tool_names,
                accept_qualified_actions=accept_qualified_actions)
            if allowed_tool_names is not None else raw_tools
        )
        await backend.create(Task(
            task_id=id_map[s["id"]], name=s["description"][:120],
            description=s["description"], assignee=assignee, requester=requester,
            parent_id=parent.task_id, tools=tools, deps=deps,
            created_by=created_by,
        ))
    return parent.task_id


def _topo_order_tasks(children: "list[Any]") -> "list[Any]":
    """Kahn's algorithm with a stable tie-break — the children's list (creation =
    LLM-emitted step) order — identical to the plan executor's
    ``_topological_order``. Returns the children in a deterministic topological
    linearization so the Task exec engine runs + aggregates units in **byte-identical
    order to the plan path** (the parity-by-construction requirement for the slice-P
    delete gate: relying on the readiness-loop's execution order to *coincide* with
    plan's topo order is fragile; computing the same order makes it a guarantee).
    Only sibling edges within ``children`` count (a dep outside the set is treated as
    already-satisfied). Cycles are impossible here — the edge-guard rejects them at
    create — so every child is emitted."""
    ids = {c.task_id for c in children}
    by_id = {c.task_id: c for c in children}
    indeg = {c.task_id: sum(1 for d in c.deps if d in ids) for c in children}
    ready = [c for c in children if indeg[c.task_id] == 0]  # preserves list order
    out: list[Any] = []
    while ready:
        current = ready.pop(0)
        out.append(current)
        for c in children:
            if current.task_id in c.deps:
                indeg[c.task_id] -= 1
                if indeg[c.task_id] == 0:
                    ready.append(by_id[c.task_id])
    return out


async def run_task_graph(
    backend: Any,
    parent_id: str,
    *,
    run_unit: RunUnit,
    on_unit_cost: "Callable[[str, float], Awaitable[None]] | None" = None,
) -> str:
    """Drive the parent's child DAG to completion in **topological order** (matching
    the plan executor's sequential ``for step in ordered``): run each unit through
    ``run_unit`` once its deps precede it, store its result, charge its cost, mark it
    completed (propagating readiness + wake events to any cross-session dependents),
    then synthesize the parent's result — the topologically-last unit whose result is
    non-empty, identical to the plan aggregator (planner.py ``reversed(ordered)``
    first-non-empty). Returns the synthesized reply.

    Topological order (not the readiness-loop's execution order) is used so the unit
    exec sequence + aggregation are byte-identical to the plan path under the same
    LLM responses — the deterministic-equivalence half of the slice-P delete gate.

    ``run_unit`` is handed each unit + the dep-results map it reads from the backend
    (the result-channel); ``on_unit_cost`` (the slice-8 ``record_task_cost``
    prod-caller) charges the unit's cost onto its Task (cap-enforcement)."""
    children = await backend.list(parent_id=parent_id)
    ordered = _topo_order_tasks(children)
    for unit in ordered:
        # Deps precede ``unit`` in topo order → already completed (+ recomputed,
        # promoting this born-blocked unit to READY). Topo order guarantees the
        # dep-results are present, so running here is valid regardless of status.
        prior_results = {}
        for dep_id in unit.deps:
            dep = await backend.get(dep_id)
            prior_results[dep_id] = (dep.result or "") if dep is not None else ""
        result_text, cost = await run_unit(unit, prior_results)
        await backend.set_result(unit.task_id, result_text)
        if on_unit_cost is not None:
            await on_unit_cost(unit.task_id, cost)
        await backend.update_status(
            unit.task_id, "completed", caller_session_id=unit.assignee)
        await backend.recompute_readiness(unit.task_id)
    final = ""
    for unit in reversed(ordered):
        t = await backend.get(unit.task_id)
        if t is not None and t.result:
            final = t.result
            break
    await backend.set_result(parent_id, final)
    return final


def make_production_run_unit(
    parent_host: Any,
    *,
    chain_id: str,
    router_model: "str | None",
    budget: Any,
    exclude_tools: "frozenset[str]" = frozenset({"plan"}),
) -> RunUnit:
    """Build the production ``run_unit``: each unit runs through a
    ``TaskExecutionHost``-narrowed ``RouterLoop`` that carries the unit's
    ``task_id``, so every LLM call the unit makes is cost-attributed to its Task
    (the RouterLoop-construction injection — never a global handle). The charged
    cost is the budget's *recorded* before/after delta for the Task, so the
    cap-counter charge is the actually-spent cost, not a re-priced estimate.

    ``exclude_tools`` drops ``plan`` by default so a unit cannot recursively
    self-decompose (the plan path's long-standing guard)."""
    from reyn.runtime.router_loop import RouterLoop
    from reyn.runtime.task_execution import TaskExecutionHost

    async def run_unit(task: Any, prior_results: "dict[str, str]") -> "tuple[str, float]":
        before = budget.task_cost_usd(task.task_id) if budget is not None else 0.0
        host = TaskExecutionHost.for_task(
            task, parent=parent_host, prior_results=prior_results)
        loop = RouterLoop(
            host=host, chain_id=chain_id, router_model=router_model,
            budget=budget, exclude_tools=set(exclude_tools),
            task_id=task.task_id,
        )
        await loop.run(user_text=task.description or task.name, history=[])
        after = budget.task_cost_usd(task.task_id) if budget is not None else 0.0
        return host.captured_text, (after - before)

    return run_unit
