"""PlanRunner — plan task lifecycle (launch / track / resume).

Extracted from ChatSession (FP-0019 follow-up, RunSpawner wave). Owns the
``running_plans`` dict and the spawn paths for both fresh plan runs
(:meth:`spawn_plan_task`) and resumed plans (:meth:`spawn_resumed_plan`).
Mirrors the SkillRunner contract: the session wires callbacks
(:func:`put_outbox`, :func:`enqueue_plan_completed`, …) so this service
never holds a direct ChatSession reference.

The router-facing entry point for spawning a plan is
``RouterHostAdapter.spawn_plan_task`` which is bound to
:meth:`spawn_plan_task` at session-init time.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.chat.outbox import OutboxMessage

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class PlanRunner:
    """Owns the ``running_plans`` dict + plan spawn / resume paths.

    Public dict (analogous to SkillRunner.running_skills): ``running_plans``
    is read by ``/plan list`` / ``/plan discard`` via
    ``session._plan_runner.running_plans``.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        put_outbox: Callable[[OutboxMessage], Awaitable[None]],
        enqueue_plan_completed: Callable[..., Awaitable[None]],
        journal: Any,
        get_router_host: Callable[[], Any],
    ) -> None:
        # ``get_router_host`` is a callable so the session can construct
        # PlanRunner BEFORE RouterHostAdapter (which receives this
        # service's ``spawn_plan_task`` as one of its own callbacks). The
        # router_host is resolved lazily at call time.
        self._agent_name = agent_name
        self._put_outbox = put_outbox
        self._enqueue_plan_completed = enqueue_plan_completed
        self._journal = journal
        self._get_router_host = get_router_host

        # Public dict — slash commands access via ``session._plan_runner.*``.
        self.running_plans: dict[str, asyncio.Task] = {}

    async def spawn_plan_task(
        self,
        *,
        plan_id: str,
        runtime: Any,
        chain_id: str,
        parent_chain_id: str | None = None,
    ) -> None:
        """Run a PlanRuntime as a background task (ADR-0023 Phase 2.1).

        Tracks the task in :attr:`running_plans` for ``/plan discard`` and
        shutdown await. On clean exit, enqueues ``plan_completed`` so the
        run loop drives router narration (symmetric with FP-0012
        skill_completed) and removes the on-disk decomposition artifact.

        Errors during ``runtime.run()`` are logged and swallowed — the task
        is fire-and-forget; the runtime's own ``finally`` emits
        ``plan_run_interrupted`` for forensic visibility, and crash cases
        leave the artifact in place for restart-time resume.

        FP-0031-A: emits a plan summary status message before execution
        starts so the user immediately sees what steps are planned.
        """
        plan = getattr(runtime, "plan", None)
        if plan is not None:
            plan_steps = getattr(plan, "steps", ())
            if plan_steps:
                plan_summary = "\n".join(
                    f"{i + 1}. {step.description}"
                    for i, step in enumerate(plan_steps)
                )
                try:
                    await self._put_outbox(OutboxMessage(
                        kind="system",
                        text=f"以下の計画で実行します:\n{plan_summary}",
                        meta={"plan_id": plan_id, "source": "plan_summary"},
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "spawn_plan_task plan_summary emit failed: %r", exc,
                    )

        async def _run_plan_task() -> None:
            from reyn.chat.planner import _is_workflow_abort
            clean_exit = False
            result: Any = None
            try:
                result = await runtime.run()
                clean_exit = True
            except BaseException as exc:
                if _is_workflow_abort(type(exc)):
                    clean_exit = True
                else:
                    logger.warning(
                        "plan task crashed for %s: %r", plan_id, exc,
                    )
            # FP-0025 C: on clean exit, enqueue plan_completed so the
            # run() loop drives router narration (symmetric with FP-0012
            # skill_completed). The router LLM synthesises step_results
            # into the final user reply.
            if clean_exit and result is not None:
                try:
                    await self._enqueue_plan_completed(
                        plan_id=plan_id,
                        chain_id=parent_chain_id or chain_id,
                        goal=result.plan_goal,
                        step_results=result.step_results,
                        step_failures=result.step_failures,
                        n_steps=result.n_steps,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "plan task enqueue_plan_completed failed for %s: %r",
                        plan_id, exc,
                    )
            # Artifact cleanup mirrors the legacy dispatch_plan_tool finally.
            if clean_exit:
                try:
                    await self._get_router_host().delete_plan_decomposition(
                        plan_id=plan_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "plan task delete_decomposition failed for %s: %r",
                        plan_id, exc,
                    )
            self.running_plans.pop(plan_id, None)

        task = asyncio.create_task(_run_plan_task())
        self.running_plans[plan_id] = task

    async def spawn_resumed_plan(
        self,
        *,
        decision: Any,
        budget: Any = None,
        router_model: str = "light",
    ) -> None:
        """Launch a PlanRuntime for a resume decision (ADR-0023 §3.4).

        Reads the decomposition artifact for ``decision.plan.plan_id``,
        constructs ``PlanRuntime(resume_plan=decision.plan)``, and
        registers the resulting task on :attr:`running_plans`. Errors
        during decomposition load surface as outbox notices and the plan
        is marked aborted (ADR-0023 §3.5 corruption fallback, even after
        the coordinator earlier-validated path).
        """
        from reyn.plan import (
            PlanRuntime,
            read_decomposition,
        )

        plan_id = decision.plan.plan_id
        agent_state_dir = (
            Path(".reyn") / "agents" / self._agent_name / "state"
        )
        try:
            decomposition = read_decomposition(agent_state_dir, plan_id)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "spawn_resumed_plan: cannot load decomposition for %s: %r",
                plan_id, exc,
            )
            try:
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=(
                        "A plan-mode reply was interrupted; the saved "
                        "decomposition could not be loaded — please "
                        "re-issue your request."
                    ),
                    meta={
                        "plan_id": plan_id,
                        "reason": "decomposition_load_failed",
                    },
                ))
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._journal.record_plan_aborted(
                    plan_id=plan_id, reason="decomposition_load_failed",
                )
            except Exception as exc2:  # noqa: BLE001
                logger.warning("plan_aborted emit failed: %r", exc2)
            return

        runtime = PlanRuntime(
            decomposition,
            host=self._get_router_host(),
            chain_id=decision.plan.chain_id,
            plan_id=plan_id,
            budget=budget,
            router_model=router_model,
            resume_plan=decision.plan,
        )

        async def _run_resumed_plan() -> None:
            try:
                await runtime.run()
            except Exception as exc:  # noqa: BLE001 — top-level swallow
                logger.warning(
                    "spawn_resumed_plan task crashed for %s: %r",
                    plan_id, exc,
                )
            finally:
                self.running_plans.pop(plan_id, None)

        task = asyncio.create_task(_run_resumed_plan())
        self.running_plans[plan_id] = task


__all__ = ["PlanRunner"]
