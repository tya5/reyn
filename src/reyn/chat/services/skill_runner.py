"""SkillRunner — skill task lifecycle (launch / track / cancel).

Extracted from ChatSession (FP-0019 Wave 1b). Owns the running_skills
dict and stdlib skill invocation path. Required as foundation for
InterventionHandler (Wave 2) and AutoResumeHandler (Wave 3).

Intervention coupling audit (FP-0019 Wave 1b):
    _run_stdlib_skill and _spawn_skill/_run_one_skill both pass a
    ChatInterventionBus to _build_agent. The bus holds a reference to
    ChatSession (for _dispatch_intervention and
    _consume_buffered_intervention_answer). SkillRunner does NOT call
    intervention methods directly; it receives a ``build_agent_fn``
    callback from the session that encapsulates the bus construction.
    Dependency direction: session wires the bus, SkillRunner only calls
    ``build_agent_fn(run_id, skill_name, *, subscribers)``. No direct
    reference to ChatInterventionBus or InterventionRegistry here.

All event emissions go through the injected ``event_log``; no silent
state changes (P6).  Business logic lives entirely here; ChatSession
delegates via :meth:`spawn`, :meth:`spawn_for_router`,
:meth:`run_stdlib`, :meth:`cancel`, :meth:`cancel_all`, and
:meth:`running_names` (P3).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

from reyn.chat.outbox import OutboxMessage
from reyn.compiler import load_dsl_skill
from reyn.events.events import EventLog
from reyn.skill.skill_paths import SkillNotFoundError, resolve_skill_path, stdlib_root

if TYPE_CHECKING:
    from reyn.agent import Agent
    from reyn.budget.budget import BudgetGateway
    from reyn.events.state_log import StateLog
    from reyn.skill.skill_registry import SkillRegistry

logger = logging.getLogger(__name__)


def _run_meta(run_id: str | None, skill_name: str | None) -> dict:
    """Standard ``meta`` payload for OutboxMessage produced inside a skill spawn."""
    if run_id is None:
        return {"skill_name": skill_name} if skill_name else {}
    return {
        "run_id": run_id,
        "skill_name": skill_name or "",
    }


class SkillRunner:
    """Skill task lifecycle service.

    Parameters
    ----------
    event_log:
        Session-scoped :class:`~reyn.events.events.EventLog`.
    agent_name:
        Name of the owning agent, used for allowlist refusal messages.
    output_language:
        Forwarded to ``agent.run`` on each skill invocation.
    mcp_servers:
        Passed through to subscribers / forwarded by the session; stored
        for future use (currently the session's ``_mcp_servers`` is
        forwarded via ``build_agent_fn``).
    allowed_skills:
        Optional allowlist. ``None`` = unrestricted.
    budget:
        :class:`~reyn.chat.services.budget_gateway.BudgetGateway` for
        pre-spawn cap checks and budget extension.
    state_log:
        Forwarded to ``agent.run`` for WAL step recording. May be
        ``None`` in test / non-chat contexts.
    build_agent_fn:
        ``(run_id, skill_name, *, subscribers) -> Agent`` — constructs a
        ready-to-run Agent wired with a per-spawn InterventionBus. The
        ``subscribers`` kwarg is optional (defaults to None inside the
        session wrapper). The session supplies this callback so
        SkillRunner never references ``ChatInterventionBus`` directly.
    put_outbox:
        Async callable ``(OutboxMessage) -> None``.
    enqueue_skill_completed:
        Async callable with keyword args ``run_id``, ``skill``,
        ``chain_id``, ``status``, ``data``.
    accumulate:
        Sync callable ``(RunResult) -> None``.
    drop_interventions_for_run:
        Sync callable ``(run_id | None) -> None``.
    get_skill_registry:
        Zero-arg callable returning ``SkillRegistry | None``.
    ask_budget_extension:
        Async callable ``(chain_id, skill_name, check) -> bool``.
    outbox:
        Raw ``asyncio.Queue`` for :meth:`run_stdlib` subscriber wiring.
    """

    def __init__(
        self,
        *,
        event_log: EventLog,
        agent_name: str,
        output_language: str | None,
        mcp_servers: dict | None,
        allowed_skills: list[str] | None,
        budget: BudgetGateway,
        state_log: StateLog | None,
        build_agent_fn: Callable[..., Agent],
        put_outbox: Callable[[OutboxMessage], Awaitable[None]],
        enqueue_skill_completed: Callable[..., Awaitable[None]],
        accumulate: Callable,
        drop_interventions_for_run: Callable[[str | None], None],
        get_skill_registry: Callable[[], SkillRegistry | None],
        ask_budget_extension: Callable[..., Awaitable[bool]],
        outbox: asyncio.Queue,
    ) -> None:
        self._events = event_log
        self._agent_name = agent_name
        self._output_language = output_language
        self._mcp_servers = mcp_servers
        self._allowed_skills = allowed_skills
        self._budget = budget
        self._state_log = state_log
        self._build_agent_fn = build_agent_fn
        self._put_outbox = put_outbox
        self._enqueue_skill_completed = enqueue_skill_completed
        self._accumulate = accumulate
        self._drop_interventions_for_run = drop_interventions_for_run
        self._get_skill_registry = get_skill_registry
        self._ask_budget_extension = ask_budget_extension
        self._outbox = outbox

        # Public dicts — slash commands (slash/skill.py, slash/tasks.py)
        # access these via ``session._skill_runner.*`` forwarding properties.
        self.running_skills: dict[str, asyncio.Task] = {}
        self.running_skills_started_at: dict[str, float] = {}
        self.running_skills_chain: dict[str, str | None] = {}

    # ── public API ────────────────────────────────────────────────────────────

    def running_names(self) -> list[str]:
        """Return a snapshot of active run_ids."""
        return list(self.running_skills.keys())

    def running_task(self, run_id: str) -> asyncio.Task | None:
        """Return the Task for *run_id*, or None if not present."""
        return self.running_skills.get(run_id)

    def chain_id_for(self, run_id: str) -> str | None:
        """Return the chain_id stashed at spawn time, or None."""
        return self.running_skills_chain.get(run_id)

    def pop_chain_id(self, run_id: str) -> str | None:
        """Pop and return the chain_id for *run_id* (used by /skill discard)."""
        return self.running_skills_chain.pop(run_id, None)

    def elapsed_for(self, run_id: str) -> float | None:
        """Return wall-clock seconds since spawn, or None."""
        started = self.running_skills_started_at.get(run_id)
        if started is None:
            return None
        return time.monotonic() - started

    async def cancel(self, run_id: str) -> None:
        """Cancel a single in-flight skill task."""
        task = self.running_skills.get(run_id)
        if task is not None and not task.done():
            task.cancel()

    async def cancel_all(self) -> None:
        """Cancel all in-flight tasks and await them (shutdown path).

        CancelledError is suppressed; any other exception is swallowed
        by ``gather(return_exceptions=True)`` — the outbox error message
        was already emitted inside the task before propagation.
        """
        for task in self.running_skills.values():
            task.cancel()
        if self.running_skills:
            await asyncio.gather(*self.running_skills.values(), return_exceptions=True)

    async def spawn(self, spec: dict, *, chain_id: str | None = None) -> None:
        """Launch a skill task and register it in the running dicts.

        Extracted from ``ChatSession._spawn_skill``.  Enforces the
        allowlist, budget cap, and FP-0003 budget extension before
        creating the asyncio Task.
        """
        skill_name = spec.get("skill")
        input_artifact = spec.get("input")
        if not skill_name or not isinstance(input_artifact, dict):
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"invalid skill spec: {spec}",
            ))
            return

        # PR15: defense-in-depth allowlist check.
        if (
            self._allowed_skills is not None
            and skill_name not in self._allowed_skills
        ):
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"skill {skill_name!r} is not in allowed_skills for agent "
                    f"{self._agent_name!r}; refused"
                ),
            ))
            self._events.emit(
                "skill_spawn_refused",
                reason="allowlist", skill=skill_name, agent=self._agent_name,
            )
            return

        # PR22: per-chain per-skill cap check.
        if chain_id is not None:
            check = self._budget.check_pre_spawn(
                chain_id=chain_id, skill=skill_name,
            )
            # FP-0003: opt-in user-approval flow on hard-limit hit.
            if (
                not check.allowed
                and check.context.get("ask_on_exceed")
                and int(check.context.get("extension_calls") or 0) > 0
            ):
                approved = await self._ask_budget_extension(
                    chain_id=chain_id,
                    skill_name=skill_name,
                    check=check,
                )
                if approved:
                    extension = int(check.context["extension_calls"])
                    new_total = self._budget.extend_chain_calls(
                        chain_id=chain_id,
                        skill=skill_name,
                        additional=extension,
                    )
                    self._events.emit(
                        "budget_extended",
                        dimension=check.hard_dimension,
                        skill=skill_name,
                        chain_id=chain_id,
                        granted=extension,
                        total_extension=new_total,
                    )
                    check = self._budget.check_pre_spawn(
                        chain_id=chain_id, skill=skill_name,
                    )
            if not check.allowed:
                from reyn.budget.budget import format_refusal_message
                self._events.emit(
                    "budget_exceeded",
                    dimension=check.hard_dimension,
                    detail=check.detail,
                    skill=skill_name,
                    chain_id=chain_id,
                )
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=format_refusal_message(check),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
                return
            for dim in check.warn_dimensions:
                from reyn.budget.budget import format_warn_message
                self._events.emit(
                    "budget_warn",
                    dimension=dim, chain_id=chain_id, skill=skill_name,
                    **check.context,
                )
                await self._put_outbox(OutboxMessage(
                    kind="status",
                    text=format_warn_message(dim, check.context),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
            self._budget.record_spawn(chain_id=chain_id, skill=skill_name)

        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"_{skill_name}_{uuid.uuid4().hex[:4]}"
        )
        self._events.emit("skill_run_spawned", run_id=run_id, skill=skill_name)
        self.running_skills_started_at[run_id] = time.monotonic()
        self.running_skills_chain[run_id] = chain_id
        await self._put_outbox(OutboxMessage(
            kind="status", text="starting…",
            meta=_run_meta(run_id, skill_name),
        ))

        task = asyncio.create_task(
            self._run_one_skill(run_id, skill_name, input_artifact, chain_id=chain_id)
        )
        self.running_skills[run_id] = task

        def _cleanup(_t: asyncio.Task, rid: str = run_id) -> None:
            self.running_skills.pop(rid, None)
            self.running_skills_started_at.pop(rid, None)
            self.running_skills_chain.pop(rid, None)
            self._drop_interventions_for_run(rid)

        task.add_done_callback(_cleanup)

    async def spawn_for_router(self, spec: dict, *, chain_id: str) -> dict:
        """Non-blocking router-side spawn entry point.

        Extracted from ``ChatSession._spawn_skill_for_router``.  Wraps
        :meth:`spawn` and returns the spawn-ack dict the router LLM
        consumes via ``invoke_skill``'s tool_result.
        """
        before = set(self.running_skills.keys())
        await self.spawn(spec, chain_id=chain_id)
        after = set(self.running_skills.keys())
        new_run_ids = after - before
        if not new_run_ids:
            return {
                "status": "error",
                "data": {
                    "error": (
                        f"skill {spec.get('skill')!r} could not be spawned "
                        f"(see prior outbox message for the specific reason)"
                    ),
                    "skill": spec.get("skill"),
                },
            }
        run_id = next(iter(new_run_ids))
        for rid in new_run_ids:
            if rid.split("_", 2)[1:2] == [str(spec.get("skill"))]:
                run_id = rid
                break
        return {
            "status": "spawned",
            "run_id": run_id,
            "chain_id": chain_id,
            "skill": spec.get("skill"),
            "note": (
                "Running in the background. "
                "I will notify you when it completes. "
                "Use /tasks to check progress."
            ),
        }

    async def run_stdlib(
        self,
        skill_name: str,
        input_artifact: dict,
        *,
        state_subdir: str,
        mcp_servers: dict | None = None,
        forward_events: bool = False,
    ):
        """Load a stdlib skill, build an Agent, run it, accumulate cost.

        Extracted from ``ChatSession._run_stdlib_skill``.  Inline stdlib
        runs (router / compactor) are NOT tracked in running_skills, so
        run_id is None — intervention cleanup won't fire on them.

        Returns the RunResult. Callers handle exceptions.
        """
        sl = stdlib_root()
        skill_md = sl / "skills" / skill_name / "skill.md"
        skill = load_dsl_skill(str(skill_md), skill_root=str(sl))

        subscribers = None
        if forward_events:
            from reyn.chat.forwarder import ChatEventForwarder
            subscribers = [ChatEventForwarder(skill_name, self._outbox)]

        agent = self._build_agent_fn(
            run_id=None, skill_name=skill_name, subscribers=subscribers,
        )
        result = await agent.run(
            skill, input_artifact,
            output_language=self._output_language,
        )
        self._accumulate(result)
        return result

    # ── internal ──────────────────────────────────────────────────────────────

    async def _run_one_skill(
        self,
        run_id: str,
        skill_name: str,
        input_artifact: dict,
        *,
        chain_id: str | None = None,
    ) -> None:
        """Core skill execution coroutine.

        Extracted from ``ChatSession._run_one_skill``.  Loads the skill,
        builds the agent with a ChatEventForwarder subscriber, runs it,
        and emits lifecycle events (P6).
        """
        meta = _run_meta(run_id, skill_name)
        try:
            skill_dir, skill_root = resolve_skill_path(skill_name)
        except SkillNotFoundError:
            self._events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"skill not found: {skill_name}",
            )
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"skill not found: {skill_name}", meta=meta,
            ))
            await self._enqueue_skill_completed(
                run_id=run_id, skill=skill_name, chain_id=chain_id,
                status="error", data={"error": f"skill not found: {skill_name}"},
            )
            return
        try:
            skill = load_dsl_skill(str(skill_dir / "skill.md"), skill_root=str(skill_root))
        except Exception as exc:
            self._events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"failed to load: {exc}",
            )
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"failed to load {skill_name}: {exc}", meta=meta,
            ))
            await self._enqueue_skill_completed(
                run_id=run_id, skill=skill_name, chain_id=chain_id,
                status="error", data={"error": f"failed to load {skill_name}: {exc}"},
            )
            return

        from reyn.chat.forwarder import ChatEventForwarder
        agent = self._build_agent_fn(
            run_id=run_id, skill_name=skill_name,
            subscribers=[ChatEventForwarder(skill_name, self._outbox, run_id=run_id)],
        )
        try:
            result = await agent.run(
                skill, input_artifact,
                output_language=self._output_language,
                chain_id=chain_id,
                skill_registry=self._get_skill_registry(),
                state_log=self._state_log,
            )
        except asyncio.CancelledError:
            await self._put_outbox(OutboxMessage(
                kind="status", text="cancelled", meta=meta,
            ))
            raise
        except Exception as exc:
            self._events.emit("skill_run_failed", run_id=run_id, skill=skill_name, error=str(exc))
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"failed: {exc}", meta=meta,
            ))
            await self._enqueue_skill_completed(
                run_id=run_id, skill=skill_name, chain_id=chain_id,
                status="error", data={"error": str(exc)},
            )
            return

        if result.status == "budget_exceeded":
            self._events.emit(
                "skill_run_failed",
                run_id=run_id, skill=skill_name,
                error="budget_exceeded",
            )
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=result.error or "budget exceeded",
                meta=meta,
            ))
            await self._enqueue_skill_completed(
                run_id=run_id, skill=skill_name, chain_id=chain_id,
                status="budget_exceeded",
                data={"error": result.error or "budget exceeded"},
            )
            return

        self._accumulate(result)
        self._events.emit(
            "skill_run_completed", run_id=run_id, skill=skill_name, status=result.status,
        )
        await self._enqueue_skill_completed(
            run_id=run_id, skill=skill_name, chain_id=chain_id,
            status=result.status or "finished",
            data=result.data or {},
        )


__all__ = ["SkillRunner"]
