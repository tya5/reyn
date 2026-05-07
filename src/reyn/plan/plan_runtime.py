"""PlanRuntime — peer to OSRuntime for plan-mode execution (ADR-0023 §3.4).

Step 5 of the migration path: a thin wrapper around the existing
:func:`execute_plan` free function. Subsequent steps (6 + 7) move the
execution body into ``PlanRuntime.run`` itself and add the resume path
(``resume_plan`` parameter, memo replay, child classification).

This step is intentionally additive: ``PlanRuntime.run`` simply calls
``execute_plan`` with the same arguments. Phase 1 tests for plan-mode
remain unchanged. Callers that want to start using ``PlanRuntime`` can
do so without behavior change.

API contract is fixed here (= ADR-0023 §3.4) so Step 6 can migrate
``dispatch_plan_tool`` against a stable shape:

.. code-block:: python

    runtime = PlanRuntime(
        plan, host=parent_host, chain_id=cid,
        plan_id=None,                 # auto-allocate uuid4-hex[:8]
        budget=None, router_model="light",
        resume_plan=None,             # None = fresh run
    )
    result = await runtime.run()      # PlanExecutionResult
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from reyn.chat.planner import (
    Plan,
    PlanExecutionResult,
    execute_plan,
)

if TYPE_CHECKING:
    from reyn.chat.router_loop import RouterLoopHost


@dataclass(frozen=True)
class PlanResumePlan:
    """Resume directive for ``PlanRuntime`` (ADR-0023 §3.2).

    Step 5 ships only the dataclass shape — Step 7 lands the analyzer
    that produces it and the runtime memoization that consumes it.
    Until then ``resume_plan=None`` is the only meaningful value.

    Fields kept minimal in Step 5; richer ``PlanStepState`` substructure
    is layered in Step 7.
    """

    plan_id: str
    chain_id: str
    goal: str
    committed_step_ids: frozenset[str] = frozenset()
    pending_step_ids: tuple[str, ...] = ()
    step_results: dict[str, str] | None = None  # populated by analyzer
    in_flight_child_step_id: str | None = None


class PlanRuntime:
    """Plan-mode execution runtime — peer to OSRuntime (ADR-0023 §3.4).

    Step 5 cut: ``run()`` literally calls :func:`execute_plan`. The
    additional constructor arguments (``plan_id``, ``resume_plan``)
    are accepted but ignored — they shape the API for Steps 6/7.
    """

    def __init__(
        self,
        plan: Plan,
        *,
        host: "RouterLoopHost",
        chain_id: str,
        plan_id: str | None = None,
        budget: Any = None,
        router_model: str = "light",
        resume_plan: PlanResumePlan | None = None,
    ) -> None:
        self._plan = plan
        self._host = host
        self._chain_id = chain_id
        self._plan_id = plan_id  # None → execute_plan auto-allocates
        self._budget = budget
        self._router_model = router_model
        self._resume_plan = resume_plan

    @property
    def plan(self) -> Plan:
        return self._plan

    @property
    def chain_id(self) -> str:
        return self._chain_id

    @property
    def plan_id(self) -> str | None:
        return self._plan_id

    @property
    def resume_plan(self) -> PlanResumePlan | None:
        return self._resume_plan

    async def run(self) -> PlanExecutionResult:
        """Execute the plan.

        Step 6: threads ``plan_id`` from the constructor through to
        ``execute_plan`` (= caller-allocated when ``dispatch_plan_tool``
        wrote the decomposition artifact first). ``resume_plan`` is
        accepted but still inert — Step 7 wires the memo replay path.
        """
        return await execute_plan(
            self._plan,
            parent_host=self._host,
            chain_id=self._chain_id,
            budget=self._budget,
            router_model=self._router_model,
            plan_id=self._plan_id,
        )


__all__ = [
    "PlanResumePlan",
    "PlanRuntime",
]
