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

from typing import TYPE_CHECKING, Any

from reyn.chat.planner import (
    Plan,
    PlanExecutionResult,
    execute_plan,
)
from reyn.plan.plan_resume_analyzer import PlanResumePlan

if TYPE_CHECKING:
    from reyn.chat.router_loop import RouterLoopHost
    from reyn.config import PlannerStepCompactionConfig
    from reyn.services.compaction.engine import CompactionEngine


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
        step_max_iterations: int | None = None,
        retry_limit: int | None = None,
        on_limit: Any = None,
        intervention_bus: Any = None,
        compaction_engine: "CompactionEngine | None" = None,
        step_compaction_cfg: "PlannerStepCompactionConfig | None" = None,
    ) -> None:
        self._plan = plan
        self._host = host
        self._chain_id = chain_id
        self._plan_id = plan_id  # None → execute_plan auto-allocates
        self._budget = budget
        self._router_model = router_model
        self._resume_plan = resume_plan
        self._step_max_iterations = step_max_iterations
        self._retry_limit = retry_limit
        self._on_limit = on_limit
        self._intervention_bus = intervention_bus
        self._compaction_engine = compaction_engine
        self._step_compaction_cfg = step_compaction_cfg

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

        Step 7: threads ``resume_plan`` through to ``execute_plan`` for
        memo replay of completed/failed steps. ``resume_plan=None`` keeps
        the fresh-run path unchanged.
        """
        return await execute_plan(
            self._plan,
            parent_host=self._host,
            chain_id=self._chain_id,
            budget=self._budget,
            router_model=self._router_model,
            plan_id=self._plan_id,
            resume_plan=self._resume_plan,
            step_max_iterations=self._step_max_iterations,
            retry_limit=self._retry_limit,
            on_limit=self._on_limit,
            intervention_bus=self._intervention_bus,
            compaction_engine=self._compaction_engine,
            step_compaction_cfg=self._step_compaction_cfg,
        )


__all__ = [
    "PlanResumePlan",
    "PlanRuntime",
]
