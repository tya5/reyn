"""SkillRunner — skill task lifecycle (launch / track / cancel).

Owns the running_skills dict and the stdlib skill invocation path; the Session
delegates skill spawns to it.

Intervention coupling:
    _run_stdlib_skill and _spawn_skill/_run_one_skill both pass a
    ChatInterventionBus to _build_agent. The bus holds a reference to
    Session (for _dispatch_intervention and
    consume_buffered_intervention_answer). SkillRunner does NOT call
    intervention methods directly; it receives a ``build_agent_fn``
    callback from the session that encapsulates the bus construction.
    Dependency direction: session wires the bus, SkillRunner only calls
    ``build_agent_fn(run_id, skill_name, *, subscribers)``. No direct
    reference to ChatInterventionBus or InterventionRegistry here.

All event emissions go through the injected ``event_log``; no silent
state changes (P6).  Business logic lives entirely here; Session
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

from reyn.core.compiler import load_dsl_skill
from reyn.core.events.events import EventLog
from reyn.skill.skill_outbound import SkillOutboundMessage
from reyn.skill.skill_paths import SkillNotFoundError, resolve_skill_path, stdlib_root

if TYPE_CHECKING:
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.budget.budget import BudgetGateway
    from reyn.schemas.models import Skill
    from reyn.skill.skill_registry import SkillRegistry
    from reyn.skill.skill_runtime import SkillRuntime

logger = logging.getLogger(__name__)


def _run_meta(run_id: str | None, skill_name: str | None) -> dict:
    """Standard ``meta`` payload for SkillOutboundMessage produced inside a skill spawn."""
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
        Session-scoped :class:`~reyn.core.events.events.EventLog`.
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
        :class:`~reyn.runtime.services.budget_gateway.BudgetGateway` for
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
        Async callable ``(SkillOutboundMessage) -> None``.
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
    make_subscribers:
        ``(skill_name, run_id=None) -> list`` — builds the chat-event
        subscribers for a spawn (the session supplies a factory that
        constructs the runtime ``ChatEventForwarder``, so SkillRunner never
        imports it — #1794 layer direction).
    format_refusal / format_warn:
        Budget message formatters (``(check) -> str`` / ``(dim, context) ->
        str``); the session supplies the runtime ``reyn.runtime.budget``
        formatters so SkillRunner stays free of that import.
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
        build_agent_fn: Callable[..., SkillRuntime],
        put_outbox: Callable[[SkillOutboundMessage], Awaitable[None]],
        enqueue_skill_completed: Callable[..., Awaitable[None]],
        accumulate: Callable,
        drop_interventions_for_run: Callable[[str | None], None],
        get_skill_registry: Callable[[], SkillRegistry | None],
        ask_budget_extension: Callable[..., Awaitable[bool]],
        make_subscribers: Callable[..., list],
        format_refusal: Callable[..., str],
        format_warn: Callable[..., str],
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
        # #1794 S3: DI'd runtime-boundary seams so reyn.skill.skill_runner takes
        # no executed reyn.runtime dependency (the layer invariant):
        #   make_subscribers → ChatEventForwarder construction (forwarder is a
        #     runtime object; Session supplies the factory).
        #   format_refusal / format_warn → budget message formatters (take a
        #     runtime BudgetCheck; kept type-opaque here via the callbacks).
        self._make_subscribers = make_subscribers
        self._format_refusal = format_refusal
        self._format_warn = format_warn

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

    async def wait_for_completion(self, timeout_sec: float = 30.0) -> None:
        """Wait up to *timeout_sec* for all in-flight skill tasks to finish.

        Does NOT cancel tasks — allows background LLM calls to complete
        naturally so ``skill_run_completed`` is emitted instead of
        ``skill_run_interrupted``.  After the timeout, still-running tasks
        are left in place (the caller should follow up with ``cancel_all``).

        Used by ``_drain_on_shutdown`` before the hard cancel so that a
        skill whose LLM call is nearly done gets the chance to land its
        P6 ``skill_run_completed`` event.
        """
        if not self.running_skills:
            return
        tasks = list(self.running_skills.values())
        try:
            await asyncio.wait(tasks, timeout=timeout_sec)
        except Exception:
            # asyncio.wait itself should never raise, but guard defensively.
            pass

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

    async def spawn(self, spec: dict, *, chain_id: str | None = None) -> "dict | None":
        """Launch a skill task and register it in the running dicts.

        Extracted from ``Session._spawn_skill``.  Enforces the
        allowlist, budget cap, FP-0003 budget extension, and pre-spawn
        input_schema validation before creating the asyncio Task.

        Returns ``None`` on successful spawn (= caller relies on
        ``self.running_skills`` for the run_id). Returns a structured
        error dict when pre-spawn checks reject the spawn before any
        asyncio task is created — currently only the input_schema
        validation path uses this return channel; other refusals (=
        allowlist, budget) keep the legacy None-return + outbox-error
        path for backward compat with non-router callers.
        """
        skill_name = spec.get("skill")
        input_artifact = spec.get("input")
        if not skill_name or not isinstance(input_artifact, dict):
            await self._put_outbox(SkillOutboundMessage(
                kind="error", text=f"invalid skill spec: {spec}",
            ))
            return None

        # PR15: defense-in-depth allowlist check.
        if (
            self._allowed_skills is not None
            and skill_name not in self._allowed_skills
        ):
            await self._put_outbox(SkillOutboundMessage(
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
            return None

        # PR22: per-chain per-skill cap check.
        if chain_id is not None:
            check = self._budget.check_pre_spawn(
                chain_id=chain_id, skill=skill_name,
            )
            # FP-0005 (#1877): on a hard-limit hit, a dimension with a
            # configured extension amount participates in the unified
            # ``safety.on_limit`` flow (interactive=ask / auto_extend=bounded
            # / unattended=deny — decided inside ``_ask_budget_extension``).
            # ``extension_calls == 0`` (default) = nothing to grant → the
            # refusal stays hard regardless of mode.
            if (
                not check.allowed
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
                self._events.emit(
                    "budget_exceeded",
                    dimension=check.hard_dimension,
                    detail=check.detail,
                    skill=skill_name,
                    chain_id=chain_id,
                )
                await self._put_outbox(SkillOutboundMessage(
                    kind="error",
                    text=self._format_refusal(check),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
                return None
            for dim in check.warn_dimensions:
                self._events.emit(
                    "budget_warn",
                    dimension=dim, chain_id=chain_id, skill=skill_name,
                    **check.context,
                )
                await self._put_outbox(SkillOutboundMessage(
                    kind="status",
                    text=self._format_warn(dim, check.context),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
            self._budget.record_spawn(chain_id=chain_id, skill=skill_name)

        # Pre-spawn input_schema validation. Loads the skill the same way
        # ``_run_one_skill`` does, validates ``input_artifact`` against the
        # entry phase's input_schema, and rejects synchronously with a
        # structured error before any asyncio task is created.
        #
        # Why pre-spawn: post-spawn validation arrives at the router LLM
        # as an async ``[task_completed] kind=skill status=error`` message
        # that's temporally separated from the originating invoke_action
        # call. Weak-tier LLMs struggle to correlate the failure back to
        # the original args + retry — they default to summarizing the
        # error to the user. Validating sync at spawn time keeps the
        # error in the same tool_result round-trip as the wrong args, so
        # the LLM can react with full local context. Also avoids burning
        # async-task setup cost on inputs that can never run.
        try:
            skill_dir, skill_root = resolve_skill_path(skill_name)
            _skill_for_validation = load_dsl_skill(
                str(skill_dir / "skill.md"), skill_root=str(skill_root),
            )
        except SkillNotFoundError as exc:
            self._events.emit(
                "skill_spawn_refused",
                reason="skill_not_found",
                skill=skill_name,
                detail=str(exc),
            )
            await self._put_outbox(SkillOutboundMessage(
                kind="error",
                text=f"skill not found: {skill_name}",
                meta={"chain_id": chain_id, "skill": skill_name},
            ))
            return {
                "status": "error",
                "data": {
                    "kind": "spawn_refused",
                    "reason": "skill_not_found",
                    "skill": skill_name,
                    "error": str(exc),
                },
            }
        except Exception as exc:
            # Skill md parse / compile error — surface as spawn refusal
            # so the LLM sees a structured failure instead of an async
            # crash inside _run_one_skill.
            self._events.emit(
                "skill_spawn_refused",
                reason="skill_load_error",
                skill=skill_name,
                detail=str(exc),
            )
            await self._put_outbox(SkillOutboundMessage(
                kind="error",
                text=f"failed to load {skill_name}: {exc}",
                meta={"chain_id": chain_id, "skill": skill_name},
            ))
            return {
                "status": "error",
                "data": {
                    "kind": "spawn_refused",
                    "reason": "skill_load_error",
                    "skill": skill_name,
                    "error": str(exc),
                },
            }

        entry_schema = getattr(_skill_for_validation, "entry_input_schema", None)
        if entry_schema:
            import jsonschema
            try:
                jsonschema.validate(input_artifact, entry_schema)
            except jsonschema.ValidationError as exc:
                # Build the structured error response with a schema_hint
                # the LLM can use directly on its next turn.
                schema_hint = {
                    "skill": skill_name,
                    "input_schema": entry_schema,
                    "retry_hint": (
                        "Re-emit invoke_action with input matching "
                        "input_schema above."
                    ),
                }
                self._events.emit(
                    "skill_spawn_refused",
                    reason="input_schema_violation",
                    skill=skill_name,
                    detail=exc.message,
                )
                await self._put_outbox(SkillOutboundMessage(
                    kind="error",
                    text=(
                        f"input validation failed for skill "
                        f"{skill_name!r}: {exc.message}"
                    ),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
                return {
                    "status": "error",
                    "data": {
                        "kind": "spawn_refused",
                        "reason": "input_schema_violation",
                        "skill": skill_name,
                        "validation_error": exc.message,
                        "schema_hint": schema_hint,
                    },
                }

        # tui-coder finding #1 fix (2026-05-28): use the OS-level canonical
        # run_id form via SkillRuntime._make_run_id. Prior bespoke construction
        # here added a `_4-hex` suffix that the agent / events layer did
        # NOT use, leaving the same skill run with 2 run_id forms in
        # flight — TUI `remove_async_task(run_id)` then failed to find rows
        # by key. Funneling through the canonical eliminates the
        # cross-layer mismatch class.
        from reyn.skill.skill_runtime import SkillRuntime as _SkillRuntime
        run_id = _SkillRuntime._make_run_id(skill_name)
        self._events.emit("skill_run_spawned", run_id=run_id, skill=skill_name)
        self.running_skills_started_at[run_id] = time.monotonic()
        self.running_skills_chain[run_id] = chain_id
        await self._put_outbox(SkillOutboundMessage(
            kind="status", text="starting…",
            meta=_run_meta(run_id, skill_name),
        ))

        task = asyncio.create_task(
            self._run_one_skill(
                run_id, skill_name, input_artifact,
                chain_id=chain_id,
                pre_loaded_skill=_skill_for_validation,
            )
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

        Extracted from ``Session._spawn_skill_for_router``.  Wraps
        :meth:`spawn` and returns the spawn-ack dict the router LLM
        consumes via ``invoke_skill``'s tool_result.

        When :meth:`spawn` rejects pre-spawn (= input_schema violation
        / skill not found / load error), the structured error dict it
        returns is forwarded verbatim so the router LLM sees a sync
        tool_result with schema_hint in the same turn as its wrong
        invoke_action call.
        """
        before = set(self.running_skills.keys())
        spawn_result = await self.spawn(spec, chain_id=chain_id)
        if isinstance(spawn_result, dict):
            return spawn_result
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

        Extracted from ``Session._run_stdlib_skill``.  Inline stdlib
        runs (router / compactor) are NOT tracked in running_skills, so
        run_id is None — intervention cleanup won't fire on them.

        Returns the RunResult. Callers handle exceptions.
        """
        sl = stdlib_root()
        skill_md = sl / "skills" / skill_name / "skill.md"
        skill = load_dsl_skill(str(skill_md), skill_root=str(sl))

        subscribers = None
        if forward_events:
            subscribers = self._make_subscribers(skill_name)

        agent = self._build_agent_fn(
            run_id=None, skill_name=skill_name, subscribers=subscribers,
        )
        result = await agent.run(
            skill, input_artifact,
            output_language=self._output_language,
        )
        self._accumulate(result)
        return result

    async def run_skill_awaitable(self, spec: dict, *, chain_id: str) -> dict:
        """Run a user-invoked skill to completion and return its tool-result dict.

        Used by the router's ``invoke_skill`` tool when the skill is dispatched
        in blocking mode (= synchronous tool_result instead of the spawn-ack
        path that :meth:`spawn_for_router` produces). Enforces the same
        allowlist + budget pre-checks as :meth:`spawn`, then loads the
        skill, builds an Agent, runs it, accumulates cost, and returns:

            {"status": "finished" | "error", "data": <final_output>}

        FP-0011: narration is the router LLM's responsibility — this method
        does NOT push to outbox. The caller returns the dict to the router
        loop which lets the LLM narrate from ``data`` on its next turn.
        """
        skill_name = spec.get("skill")
        input_artifact = spec.get("input")
        if not skill_name or not isinstance(input_artifact, dict):
            return {
                "status": "error",
                "data": {"error": f"invalid skill spec: {spec}"},
            }

        # PR15: allowlist check — same defense as spawn().
        if (
            self._allowed_skills is not None
            and skill_name not in self._allowed_skills
        ):
            self._events.emit(
                "skill_spawn_refused",
                reason="allowlist", skill=skill_name, agent=self._agent_name,
            )
            return {
                "status": "error",
                "data": {
                    "error": (
                        f"skill {skill_name!r} is not in allowed_skills for "
                        f"agent {self._agent_name!r}; refused"
                    )
                },
            }

        # PR22: budget cap pre-check.
        check = self._budget.check_pre_spawn(chain_id=chain_id, skill=skill_name)
        # FP-0005 (#1880): unify with the chat-spawn path (:268, #1877) — a hard hit
        # on a dimension with a configured extension participates in the unified
        # ``safety.on_limit`` flow (interactive=ask / auto_extend=bounded /
        # unattended=deny — decided inside ``_ask_budget_extension``). Programmatic
        # spawns have no TTY → the bus is None → the interactive branch falls to a
        # ``no_bus`` deny (the existing fail-closed); auto_extend is bus-independent.
        # ``extension_calls == 0`` (default) → nothing to grant → the refusal stays
        # hard regardless of mode (default-config behavior is byte-identical).
        if (
            not check.allowed
            and int(check.context.get("extension_calls") or 0) > 0
        ):
            approved = await self._ask_budget_extension(
                chain_id=chain_id, skill_name=skill_name, check=check,
            )
            if approved:
                extension = int(check.context["extension_calls"])
                new_total = self._budget.extend_chain_calls(
                    chain_id=chain_id, skill=skill_name, additional=extension,
                )
                self._events.emit(
                    "budget_extended",
                    dimension=check.hard_dimension,
                    skill=skill_name,
                    chain_id=chain_id,
                    granted=extension,
                    total_extension=new_total,
                )
                check = self._budget.check_pre_spawn(chain_id=chain_id, skill=skill_name)
        if not check.allowed:
            self._events.emit(
                "budget_exceeded",
                dimension=check.hard_dimension,
                detail=check.detail,
                skill=skill_name,
                chain_id=chain_id,
            )
            return {
                "status": "error",
                "data": {"error": check.detail or "budget exceeded"},
            }
        self._budget.record_spawn(chain_id=chain_id, skill=skill_name)

        # tui-coder finding #1 fix (2026-05-28): canonical run_id form via
        # SkillRuntime._make_run_id (see sibling spawn site above for full
        # rationale).
        from reyn.skill.skill_runtime import SkillRuntime as _SkillRuntime
        run_id = _SkillRuntime._make_run_id(skill_name)
        self._events.emit(
            "skill_run_spawned", run_id=run_id, skill=skill_name,
        )

        # P6 audit completeness: when skill_run_spawned fires we must pair
        # it with a terminal event (skill_run_failed or skill_run_completed)
        # on every exit path below.
        try:
            skill_dir, skill_root = resolve_skill_path(skill_name)
        except SkillNotFoundError:
            self._events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"skill not found: {skill_name}",
            )
            return {
                "status": "error",
                "data": {"error": f"skill not found: {skill_name}"},
            }

        try:
            skill = load_dsl_skill(
                str(skill_dir / "skill.md"), skill_root=str(skill_root),
            )
        except Exception as exc:
            self._events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"failed to load: {exc}",
            )
            return {
                "status": "error",
                "data": {"error": f"failed to load {skill_name}: {exc}"},
            }

        agent = self._build_agent_fn(
            run_id=run_id, skill_name=skill_name,
            subscribers=self._make_subscribers(skill_name, run_id),
        )

        # Issue #214: forward the plan_step ContextVar so a blocking
        # router invoke_skill inside a plan step's sub-loop stamps the
        # spawned skill's events with the originating step.
        _plan_step = None  # plan-step context removed with plan (#1953)
        try:
            result = await agent.run(
                skill, input_artifact,
                output_language=self._output_language,
                chain_id=chain_id,
                skill_registry=self._get_skill_registry(),
                state_log=self._state_log,
                plan_step=_plan_step,
            )
        except asyncio.CancelledError:
            return {"status": "error", "data": {"error": "cancelled"}}
        except Exception as exc:
            self._events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=str(exc),
            )
            return {"status": "error", "data": {"error": str(exc)}}

        if result.status == "budget_exceeded":
            self._events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error="budget_exceeded",
            )
            return {
                "status": "error",
                "data": {"error": result.error or "budget exceeded"},
            }

        self._accumulate(result)
        self._events.emit(
            "skill_run_completed", run_id=run_id, skill=skill_name,
            status=result.status,
        )

        # FP-0011: the router LLM narrates inline on its post-invoke_skill
        # turn — we only return the canonical tool_result envelope.
        return {
            "status": result.status or "finished",
            "data": result.data or {},
        }

    async def spawn_resumed_skill(self, decision: "object") -> None:
        """Default launcher used by ``AutoResumeHandler._resume_and_collect``.

        Loads the skill named by ``decision.plan.skill_name``, builds an
        Agent, and spawns ``SkillRuntime.run`` as a tracked asyncio task with the
        resume_plan threaded in. Separate from :meth:`spawn` so the
        auto-resume hook can be tested with a stub launcher
        (see ``tests/test_session_auto_resume.py``).
        """
        plan = decision.plan
        skill_name = plan.skill_name
        run_id = plan.run_id
        meta = _run_meta(run_id, skill_name)
        try:
            skill_dir, skill_root = resolve_skill_path(skill_name)
            skill = load_dsl_skill(
                str(skill_dir / "skill.md"), skill_root=str(skill_root),
            )
        except (SkillNotFoundError, Exception) as exc:
            self._events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"resume failed to load: {exc}",
            )
            await self._put_outbox(SkillOutboundMessage(
                kind="error", text=f"resume failed: {exc}", meta=meta,
            ))
            return

        agent = self._build_agent_fn(
            run_id=run_id, skill_name=skill_name,
            subscribers=self._make_subscribers(skill_name, run_id),
        )

        async def _runner():
            try:
                await agent.run(
                    skill, plan.skill_input,
                    output_language=self._output_language,
                    skill_registry=self._get_skill_registry(),
                    state_log=self._state_log,
                    resume_plan=plan,
                    run_id=run_id,
                )
            except asyncio.CancelledError:
                await self._put_outbox(SkillOutboundMessage(
                    kind="status", text="cancelled", meta=meta,
                ))
                raise
            except Exception as exc:  # noqa: BLE001 — surface to outbox
                self._events.emit(
                    "skill_run_failed", run_id=run_id, skill=skill_name,
                    error=str(exc),
                )
                await self._put_outbox(SkillOutboundMessage(
                    kind="error", text=f"resume failed: {exc}", meta=meta,
                ))

        self.running_skills_started_at[run_id] = time.monotonic()
        # R-D14: resumed skill_run is generally not chain-tagged (the
        # original chain has long-since either completed or been wedged
        # by the timeout watchdog). If a future re-issue path needs to
        # carry chain_id across resume, plumb it through ``decision``.
        self.running_skills_chain[run_id] = None
        await self._put_outbox(SkillOutboundMessage(
            kind="status", text="resuming…", meta=meta,
        ))
        task = asyncio.create_task(_runner())
        self.running_skills[run_id] = task

        def _cleanup(_t: asyncio.Task, rid: str = run_id) -> None:
            self.running_skills.pop(rid, None)
            self.running_skills_started_at.pop(rid, None)
            self.running_skills_chain.pop(rid, None)
            self._drop_interventions_for_run(rid)

        task.add_done_callback(_cleanup)

    # ── internal ──────────────────────────────────────────────────────────────

    async def _run_one_skill(
        self,
        run_id: str,
        skill_name: str,
        input_artifact: dict,
        *,
        chain_id: str | None = None,
        pre_loaded_skill: "Skill | None" = None,
    ) -> None:
        """Core skill execution coroutine.

        Extracted from ``Session._run_one_skill``.  Loads the skill,
        builds the agent with a ChatEventForwarder subscriber, runs it,
        and emits lifecycle events (P6).

        ``pre_loaded_skill`` lets ``spawn()`` hand off the Skill it
        already parsed for pre-spawn input_schema validation, eliminating
        the duplicate resolve_skill_path + load_dsl_skill on the success
        path (= 2x disk read + 2x YAML parse per spawn pre-fix). When
        absent (= callers other than spawn(), or future code paths) the
        load fallback below preserves original behaviour.
        """
        meta = _run_meta(run_id, skill_name)
        if pre_loaded_skill is not None:
            skill = pre_loaded_skill
        else:
            try:
                skill_dir, skill_root = resolve_skill_path(skill_name)
            except SkillNotFoundError:
                self._events.emit(
                    "skill_run_failed", run_id=run_id, skill=skill_name,
                    error=f"skill not found: {skill_name}",
                )
                await self._put_outbox(SkillOutboundMessage(
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
                await self._put_outbox(SkillOutboundMessage(
                    kind="error", text=f"failed to load {skill_name}: {exc}", meta=meta,
                ))
                await self._enqueue_skill_completed(
                    run_id=run_id, skill=skill_name, chain_id=chain_id,
                    status="error", data={"error": f"failed to load {skill_name}: {exc}"},
                )
                return

        agent = self._build_agent_fn(
            run_id=run_id, skill_name=skill_name,
            subscribers=self._make_subscribers(skill_name, run_id),
        )
        # B33 W6 NEW-1 fix: track the terminal (status, data) pair so
        # _enqueue_skill_completed fires even when an intermediate await
        # (e.g. _put_outbox) raises before the explicit enqueue call.
        # The finally clause guarantees the inbox message reaches the
        # session loop on every non-cancelled terminal path — regardless
        # of which sub-path (LLM-abort / phase_no_progress / budget /
        # success) produced the terminal status.
        _terminal_status: str | None = None
        _terminal_data: dict = {}
        # Issue #214: read the plan_step ContextVar set by planner.py
        # around the step's sub-RouterLoop. ContextVar propagates through
        # any asyncio.Task created within the scope, so this spawn site
        # sees the planner-set value when the skill was invoked inside a
        # plan step. None = top-level invocation (not in a plan).
        _plan_step = None  # plan-step context removed with plan (#1953)
        try:
            result = await agent.run(
                skill, input_artifact,
                output_language=self._output_language,
                chain_id=chain_id,
                skill_registry=self._get_skill_registry(),
                state_log=self._state_log,
                plan_step=_plan_step,
            )
        except asyncio.CancelledError:
            await self._put_outbox(SkillOutboundMessage(
                kind="status", text="cancelled", meta=meta,
            ))
            raise  # CancelledError: no completion enqueue (task was discarded)
        except Exception as exc:
            # WorkflowAbortedError (= LLM-abort / phase_no_progress / no
            # previous phase) and all other terminal errors land here.
            _terminal_status = "error"
            _terminal_data = {"error": str(exc)}
            self._events.emit("skill_run_failed", run_id=run_id, skill=skill_name, error=str(exc))
            try:
                await self._put_outbox(SkillOutboundMessage(
                    kind="error", text=f"failed: {exc}", meta=meta,
                ))
            except Exception:  # noqa: BLE001 — outbox failure must not suppress enqueue
                pass
            # #106: an unexpected Python exception bypasses the OS's
            # workflow_aborted emit, so ChatEventForwarder never converts
            # it into a "skill done: aborted" trace — leaving any mounted
            # SkillActivityRow spinning forever. Enqueue the trace
            # directly so the TUI finishes the row.
            try:
                await self._put_outbox(SkillOutboundMessage(
                    kind="trace", text="skill done: aborted", meta=meta,
                ))
            except Exception:  # noqa: BLE001 — same defense as above
                pass
        else:
            if result.status == "budget_exceeded":
                _terminal_status = "budget_exceeded"
                _terminal_data = {"error": result.error or "budget exceeded"}
                self._events.emit(
                    "skill_run_failed",
                    run_id=run_id, skill=skill_name,
                    error="budget_exceeded",
                )
                try:
                    await self._put_outbox(SkillOutboundMessage(
                        kind="error",
                        text=result.error or "budget exceeded",
                        meta=meta,
                    ))
                except Exception:  # noqa: BLE001
                    pass
                # #1944: like the abort branch (#106) and the success branch,
                # enqueue a "skill done:" trace so the background skill's
                # AsyncStackPanel row is removed (terminal="aborted" path:
                # the strip flashes the ✗ shape before unmounting). Without
                # this the budget-exceeded background skill ghosts too — the
                # sibling gap to the success-path ghost.
                try:
                    await self._put_outbox(SkillOutboundMessage(
                        kind="trace", text="skill done: aborted: budget exceeded",
                        meta=meta,
                    ))
                except Exception:  # noqa: BLE001
                    pass
            else:
                self._accumulate(result)
                self._events.emit(
                    "skill_run_completed", run_id=run_id, skill=skill_name, status=result.status,
                )
                # #1944: a spawned (background) skill's bottom-strip
                # AsyncStackPanel row is removed by the TUI on the
                # "skill done:" trace. The abort branch above already
                # enqueues one directly (#106); the success branch must do
                # the same — otherwise a successfully-completing background
                # skill leaves a ghost row with its elapsed counter ticking
                # forever. Explicit enqueue = completeness-by-construction
                # (every terminal branch emits "skill done:"); the TUI's
                # remove is idempotent, so this is safe even if any forwarder
                # path also delivers one.
                try:
                    await self._put_outbox(SkillOutboundMessage(
                        kind="trace", text="skill done: finished", meta=meta,
                    ))
                except Exception:  # noqa: BLE001 — same defense as the abort path
                    pass
                _terminal_status = result.status or "finished"
                _terminal_data = result.data or {}
        finally:
            # Guaranteed enqueue on all non-cancelled terminal paths.
            # _enqueue_skill_completed has its own try/except so it never
            # raises; the guard avoids a spurious enqueue on CancelledError
            # (where _terminal_status is still None).
            if _terminal_status is not None:
                await self._enqueue_skill_completed(
                    run_id=run_id, skill=skill_name, chain_id=chain_id,
                    status=_terminal_status, data=_terminal_data,
                )


__all__ = ["SkillRunner"]
