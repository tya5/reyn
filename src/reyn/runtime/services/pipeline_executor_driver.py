"""PipelineExecutorDriver — the driver-session's ExecutionDriver for async pipelines (IS-2).

The D案 execution model: ``run_pipeline_async`` spawns a dedicated
driver-session that is *born with its work-order*
(``reyn.core.pipeline.work_order`` — persisted to
``.reyn/pipeline/state/<run_id>/invocation.json`` before step 0 runs). This
class is that session's :class:`~reyn.runtime.services.execution_driver.ExecutionDriver`:
where ``RouterLoopDriver`` interprets ``run_turn(user_text, ...)`` as a user
utterance to route through the LLM, this driver treats the turn as a bare
**run/resume nudge** — ``user_text`` carries no meaning — and drives the
already-tested :class:`~reyn.core.pipeline.executor.PipelineExecutor` instead.
No protocol change: the Session's run-loop, inbox, WAL journaling and
crash-restore machinery all work on this session exactly as on a chat session,
which is the entire point (crash auto-resume rides the existing session
substrate).

One nudge = drive the run to a terminal (#2572: including the run's
``SchemaRegistry``, rebuilt from ``work_order.schema_defs`` via
``reyn.core.pipeline.serde.schema_registry_from_dict`` and threaded into
``executor.run``/``resume`` so a ``verify: schema`` step is enforced —
previously it raised ``PipelineExecutionError``/``AgentStepError`` unconditionally
because no registry ever reached the executor):

- **new vs resume** is decided by whether an R4 generation snapshot exists for
  the run (``latest_pipeline_state``): none → ``executor.run`` seeded with the
  work-order's ORIGINAL ``input`` (a resume-always shortcut would silently
  drop it — ``resume``'s no-snapshot fallback hardcodes ``initial_context=None``);
  some → ``executor.resume``, which replays completed steps from the snapshot
  (exactly-once) and continues.
- the result (or step failure) is posted to the work-order's reply address as
  a ``pipeline_result`` inbox message (mirroring how delegation returns
  ``agent_response`` — see ``Session.submit_pipeline_result``), and only THEN
  is the terminal marker (``result.json``) written. Terminal =
  "result delivered", so a crash between last step and delivery re-delivers
  on recovery: execution exactly-once, delivery at-least-once (the
  work_order module docstring states the full contract). IS-6: the inbox
  post is gated on the runtime ``notify_reply`` flag — the sync ATTACHED
  launch path sets it False (the attached caller collects the result in-band
  via ``read_result``, so a reply turn to that same session would be a
  redundant unprompted LLM turn); the terminal marker is written either way.

- IS-6 attached run: the driver threads THIS session's ``EventLog`` as the
  executor's ``events`` sink (``pipeline_step_started`` / ``_completed`` per
  step — the emit half of the seam an attached caller / the TUI subscribes to)
  and its own ``is_cancel_requested`` as the executor's ``cancel_check``, polled
  at each step boundary. #2588: a Ctrl-C hits ``cancel_inflight`` on the
  ATTACHED CALLER session (a DIFFERENT session than this driver-session), which
  by itself cancels only the caller's own turn-driver. ``run_pipeline_attached``
  bridges the gap: for the duration of the attached pump it registers THIS
  driver's ``request_cancel`` as a cancel-forward on the caller session (via
  ``Session.register_cancel_forward``), so the caller's ``cancel_inflight`` also
  flips this driver's ``_cancel_requested``. The executor then observes it at
  the next step boundary and raises ``PipelineCancelled``: the driver writes a
  TERMINAL ``cancelled`` marker (so the recovery scan never resurrects an
  intentionally-cancelled run) while LEAVING the R4 generation snapshots on
  disk (abort-now, resume-later — R6). Cancel used to reach only a
  sync/attached run (async is detached — nothing forwarded a cancel signal
  to a driver-session nobody is attached to) — proposal 0067 P4 (#3978)
  closes that gap: launch-time registration on the settle-path handle
  (``session_api._spawn_pipeline_driver_session``) captures THIS driver's
  ``request_cancel`` as the task's ``cancel`` hook (``ChainManager``'s
  ``_PendingChain.cancel``, cooperative and argument-zero, same shape as
  the sync path's forward), so ``cancel_task`` can reach a DETACHED async
  run too. ``notify_reply`` is no longer 1:1 with "was this cancelled via
  the attached path" — an async run's cancelled terminal still delivers
  (settles) via the normal ``_finish``/``_deliver`` path below.
- after terminal, the driver marks its session ephemeral so the standard
  post-turn vanish teardown (``Session._maybe_schedule_ephemeral_vanish`` →
  ``registry.remove_session``) reclaims it — the driver-session never leaks
  past its run, on the initial path and the recovered path alike.
- the poison-pipeline cap: the recovery scan durably bumps
  ``attempts.json`` before every re-wake; when the count exceeds
  ``MAX_RESUME_ATTEMPTS`` this driver's FIRST action is to terminal-fail the
  run (failure result delivered) instead of resuming — a run whose resume
  crashes the process on every restart is bounded by construction.

Tool steps dispatch through the SAME ``_make_tool_dispatch`` the sync
``run_pipeline`` tool uses (``reyn.tools.pipeline_verbs``), fed a
``ToolContext`` built from THIS session's own host adapter (events /
permission_resolver / resolver / state_log), plus (#3546) the session's LIVE
``contextual_permission`` — the TOOL-axis narrowing, passed explicitly because
this dispatch runs outside any ``RouterLoop`` and so neither RouterLoop
TOOL-axis gate is in the path. This module used to describe that context as
"⊆ the invoker's since the driver-session is spawned under the invoker's
identity"; identity carries only the NAME-keyed layers of the envelope, and
the sid-keyed per-session narrowing was measurably absent (see
``session_api._spawn_pipeline_driver_session``, step 1). #2567: ``router_state`` is a real
``RouterCallerState`` built via ``reyn.tools.types.build_resource_caller_state``
— the shared host-derived-fields factory extracted from
``RouterLoop._build_router_caller_state`` — so tool steps that resolve through
resource-category dynamic routes (mcp tools, rag corpus reads) get the SAME
mcp/rag/skills/sandbox/agent-registry/pipeline-registry wiring a live
RouterLoop turn gets. The loop-local fields (``send_to_agent`` /
``spawn_session_fn`` / ``spawn_agent_fn`` / ``topology_create_fn`` /
``chain_id`` / ``budget`` / catalog-callback / memory-callback fields) stay
``None`` here by design — there is no RouterLoop turn to own them — but
``delegate_to_agent`` / ``run_pipeline`` / ``run_pipeline_async`` are already
structurally denied for pipeline tool steps (R6 S3,
``pipeline_verbs._PIPELINE_STEP_DENY_TOOLS``), so that gap is moot for the
tool-step surface.

The driver is bound to its session AFTER construction
(``Session.set_loop_driver`` calls :meth:`bind_session` — the post-ctor
observer seam), because the session cannot exist before a driver argument
would be needed and the recovery path re-creates sessions through the plain
factory anyway.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reyn.hooks.schema_registry import build_hook_payload
from reyn.runtime.session_pure import new_chain_id

if TYPE_CHECKING:
    from reyn.core.events.state_log import StateLog
    from reyn.core.pipeline.work_order import PipelineWorkOrder
    from reyn.runtime.registry import AgentRegistry

logger = logging.getLogger(__name__)

# A8: recovery re-wakes past this count terminal-fail instead of resuming.
MAX_RESUME_ATTEMPTS = 3


async def resolve_reply_target(
    registry: "AgentRegistry", agent: str, sid: str,
) -> "tuple[Any | None, str | None]":
    """Resolve a live (agent, sid) reply target through the registry.

    Mirrors ``Session._a2a_send_response``'s routing (non-main sid -> the
    specific live session, fail-closed if not loaded and NEVER rerouted to
    main; main -> cold-load + ensure_running). Shared by the settle-path's
    two symmetric call sites — launch-time handle registration
    (``_spawn_pipeline_driver_session``) and settle-time delivery
    (``PipelineExecutorDriver._deliver``) — so a task's collection handle
    only ever exists where a delivery would actually be attempted.

    Returns ``(target, None)`` on success or ``(None, reason)`` on failure
    (never raises — the fail-safe IS the return value)."""
    if not registry.exists(agent):
        return None, f"reply agent {agent!r} no longer exists"
    if sid and sid != "main":
        target = registry.get_session(agent, sid)
        if target is None:
            return None, f"reply session ({agent!r}, {sid!r}) is not loaded"
        registry.ensure_session_running(agent, sid)
        return target, None
    target = registry.get_or_load(agent)
    await registry.ensure_running(agent)
    return target, None


class PipelineExecutorDriver:
    """ExecutionDriver that runs/resumes ONE pipeline work-order per nudge."""

    def __init__(
        self,
        work_order: "PipelineWorkOrder",
        *,
        registry: "AgentRegistry",
        state_log: "StateLog",
        notify_reply: bool = True,
    ) -> None:
        self._work_order = work_order
        self._registry = registry
        self._state_log = state_log
        self._cancel_requested = False
        self._session: Any = None
        self._router_host: Any = None
        # IS-6: whether terminal delivery posts a ``pipeline_result`` to the
        # reply address. RUNTIME-only (deliberately NOT a persisted work-order
        # field): each LAUNCH PATH sets it, and a crash DESTROYS the driver, so
        # the recovery scan re-creates a fresh driver with the recovery default
        # (True) — a crashed sync run then correctly degrades to inbox delivery.
        #   - async ``run_pipeline_async``  → True  (caller got {started}, awaits inbox)
        #   - recovery ``_rewake_pipeline_runs`` → True (the attached caller is gone)
        #   - sync attached ``run_pipeline`` → False (the caller reads the result
        #     inline via ``read_result``; a redundant reply turn would be a defect)
        self._notify_reply = notify_reply

    # ── post-ctor binding (Session.set_loop_driver calls this) ────────────────

    def bind_session(self, session: Any, router_host: Any) -> None:
        """Late-bind the owning Session + its RouterHostAdapter (the source of
        the ToolContext fields). Called by ``Session.set_loop_driver``."""
        self._session = session
        self._router_host = router_host

    # ── ExecutionDriver protocol ───────────────────────────────────────────────

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        """Drive the work-order to terminal. ``user_text`` is a meaningless
        nudge payload (D案) and is ignored. Idempotent: a nudge after terminal
        no-ops (delivery is at-least-once, so double wakes are expected)."""
        from reyn.core.events.pipeline_recovery import latest_pipeline_state
        from reyn.core.pipeline.executor import (
            PipelineCancelled,
            PipelineExecutionError,
            PipelineExecutor,
        )
        from reyn.core.pipeline.serde import pipeline_from_dict, schema_registry_from_dict
        from reyn.core.pipeline.work_order import has_result, read_resume_attempts

        wo = self._work_order
        run_dir = self._run_dir()
        if has_result(run_dir):
            return  # already terminal — spurious/duplicate nudge

        # A8 poison cap: the recovery scan bumped attempts.json durably before
        # this wake; past the cap, fail terminally BEFORE touching the executor.
        attempts = read_resume_attempts(run_dir)
        if attempts > MAX_RESUME_ATTEMPTS:
            await self._finish(
                status="failed",
                error=(
                    f"pipeline run {wo.run_id!r} exhausted its resume budget "
                    f"({attempts - 1} recovery attempts > cap {MAX_RESUME_ATTEMPTS}) "
                    "— giving up instead of crash-looping."
                ),
            )
            return

        pipeline = pipeline_from_dict(wo.pipeline)
        # #2572: rebuild the launch's SchemaRegistry from the persisted
        # work-order field — the SAME recovery source as ``pipeline`` above
        # (a FILE, not a live constructor arg), so a ``verify: schema`` step
        # is enforced identically on the original run and on a re-created,
        # crash-resumed driver-session (the recovery scan builds this driver
        # from ``work_order`` alone — see ``AgentRegistry._rewake_pipeline_runs``).
        schema_registry = schema_registry_from_dict(wo.schema_defs)
        executor = PipelineExecutor()
        # IS-6: emit step-boundary progress to THIS session's EventLog (an
        # attached caller subscribes to it) and poll THIS driver's cooperative
        # cancel flag at each step boundary.
        events = getattr(self._router_host, "events", None)
        # R7: a `call` step resolves its target through the session's
        # PipelineRegistry (the same host-derived registry the sync run_pipeline
        # tool resolves against). None-safe: a pipeline with no `call` step never
        # touches it; a `call` with no registry available fails the step cleanly.
        pipeline_registry = self._pipeline_registry()
        try:
            if latest_pipeline_state(wo.run_id, self._state_log) is None:
                # Fresh run (or crashed before the first R4 snapshot): seed the
                # ORIGINAL work-order input — resume()'s fallback would lose it.
                result = await executor.run(
                    pipeline,
                    dict(wo.input) if wo.input else None,
                    tool_dispatch=await self._make_dispatch(),
                    state_log=self._state_log,
                    run_id=wo.run_id,
                    registry=self._registry,
                    default_identity=wo.reply_to_agent,
                    pipeline_registry=pipeline_registry,
                    events=events,
                    cancel_check=self.is_cancel_requested,
                    schema_registry=schema_registry,
                    max_fan_out_depth=self._registry.max_pipeline_fan_out_depth,
                    max_pipeline_spawns=self._registry.max_pipeline_spawns,
                    # #2769: thread THIS driver-session down to any agent-step's
                    # run_agent_step so its ask_user / permission / present reach the
                    # pipeline originator (via BridgeToParent + the #2735 transitive
                    # bridge). When this driver is itself detached (async / headless
                    # launch), its OWN intervention_bridge is AuditOnly, so the
                    # transitive walk terminates in a fail-closed typed refusal at this
                    # driver hop — the agent-step is never left able to self-allow.
                    invoker_session=self._session,
                )
            else:
                result = await executor.resume(
                    wo.run_id,
                    pipeline=pipeline,
                    tool_dispatch=await self._make_dispatch(),
                    state_log=self._state_log,
                    registry=self._registry,
                    default_identity=wo.reply_to_agent,
                    pipeline_registry=pipeline_registry,
                    events=events,
                    cancel_check=self.is_cancel_requested,
                    schema_registry=schema_registry,
                    max_fan_out_depth=self._registry.max_pipeline_fan_out_depth,
                    max_pipeline_spawns=self._registry.max_pipeline_spawns,
                    # #2769: same invoker threading on the resume path (see run above).
                    invoker_session=self._session,
                )
        except PipelineCancelled as exc:
            # IS-6: an intentional stop at a step boundary. TERMINAL (so the
            # recovery scan never zombie-resurrects a user-cancelled run) but
            # the R4 generation snapshots are LEFT ON DISK — abort-now, yet a
            # future explicit-resume tool could continue from ``step_index``.
            # Two reachable sources today: a Ctrl-C on the sync/attached path
            # (``notify_reply=False`` — the attached caller reads
            # ``status=cancelled`` inline via ``read_result``, no inbox turn)
            # and, since proposal 0067 P4 (#3978), ``cancel_task`` reaching a
            # DETACHED async run's ``request_cancel`` hook via the settle-path
            # handle (``notify_reply=True`` — this ``_finish`` call below
            # settles/delivers exactly like any other terminal status).
            await self._finish(
                status="cancelled",
                error=str(exc),
                output={"cancelled_at_step_index": exc.step_index},
            )
            return
        except PipelineExecutionError as exc:
            await self._finish(status="failed", error=str(exc))
            return
        await self._finish(
            status="ok", output=result.pipe_data, named_stores=result.named_stores,
        )

    def is_cancel_requested(self) -> bool:
        """Cooperative cancel flag. IS-6: passed as the executor's
        ``cancel_check`` — polled at each step BOUNDARY, so a True reading stops
        the run cleanly before the next step (see ``run_turn``)."""
        return self._cancel_requested

    def request_cancel(self) -> None:
        """Record a cancel request. #2588: reached from the ATTACHED CALLER
        session's ``cancel_inflight`` — ``run_pipeline_attached`` registers this
        method as a cancel-forward on that caller session for the attached run's
        duration, so a Ctrl-C on the caller flips THIS driver-session's flag. The
        executor observes it at the next step boundary via ``is_cancel_requested``
        and raises ``PipelineCancelled``."""
        self._cancel_requested = True

    async def _check_cap(self, user_text: str) -> None:
        """No-op: the router invocation cap has no meaning for a deterministic
        pipeline turn (the executor's own step list is the bound)."""
        return None

    @property
    def cancel_event(self) -> None:
        """#2813: no interactive-turn cancel_event concept here — cancellation is
        the bare ``_cancel_requested`` bool polled at step boundaries (see
        ``request_cancel``'s docstring). Any bounded MCP/network call this
        driver's turn makes falls back to running to its own internal timeout on
        Ctrl-C, same as every pre-#2813 caller with no cancel_event to pass."""
        return None

    # ── internals ──────────────────────────────────────────────────────────────

    def _pipeline_registry(self) -> Any:
        """R7: the session's ``PipelineRegistry`` (or None) — the resolution
        source a ``call`` step's target name is looked up in, via the host
        adapter's ``get_pipeline_registry()`` (the SAME registry the sync
        ``run_pipeline`` tool resolves against through
        ``RouterCallerState.pipeline_registry``). None-safe: a pipeline with no
        ``call`` step never touches it."""
        host = self._router_host
        getter = getattr(host, "get_pipeline_registry", None) if host is not None else None
        return getter() if getter is not None else None

    def _run_dir(self) -> "Path":
        from reyn.core.events.config_recovery import reyn_root
        from reyn.core.pipeline.work_order import pipeline_run_dir

        root = reyn_root(self._state_log.path)
        if root is None:  # construction guards this; defend against re-pathing
            raise RuntimeError(
                "PipelineExecutorDriver requires a .reyn-anchored StateLog "
                f"(got {self._state_log.path!r})"
            )
        return pipeline_run_dir(root, self._work_order.run_id)

    async def _make_dispatch(self) -> Any:
        """The SAME tool-step dispatch the sync ``run_pipeline`` tool builds
        (``pipeline_verbs._make_tool_dispatch``), fed a ToolContext from THIS
        session's host adapter. #2567: ``router_state`` is now a real
        ``RouterCallerState`` built via ``build_resource_caller_state(host)``
        — the same host-derived mcp/rag/skills/sandbox/agent-registry/
        pipeline-registry resource wiring a live RouterLoop turn gets (S3
        pipeline-step tool deny is unaffected — it gates on the tool name
        string before any router_state access)."""
        from reyn.tools.pipeline_verbs import _make_tool_dispatch
        from reyn.tools.types import ToolContext, build_resource_caller_state

        host = self._router_host
        if host is None:
            raise RuntimeError(
                "PipelineExecutorDriver is not bound to a session — "
                "Session.set_loop_driver(driver) must run before the first nudge."
            )
        ctx = ToolContext(
            events=host.events,
            permission_resolver=getattr(host, "permission_resolver", None),
            workspace=getattr(host, "workspace", None),
            caller_kind="router",
            router_state=await build_resource_caller_state(host),
            resolver=getattr(host, "resolver", None),
            hot_reloader=getattr(host, "hot_reloader", None),
            state_log=getattr(host, "state_log", None),
            agent_name=getattr(host, "agent_name", None),  # #2088: scope-aware hooks_add
            # #4215①: this driver IS bound to a live session's host (the
            # guard above requires it) — session_state_dir is readily
            # available there, so a pipeline `tool: hooks_add` step lands
            # in THIS driver-session's own isolated layer, not the global
            # one. Found via architect's reachability sweep (#4215):
            # missing this field here is the same gap pipe.py's
            # session-less `reyn pipe run` has, but THIS site has no
            # excuse — the value already exists on `host`.
            #
            # #4244 note: hooks_add — the only current reader of
            # ctx.session_state_dir anywhere in src/ — is now denied at
            # THIS exact dispatch point (pipeline_verbs._PIPELINE_STEP_
            # DENY_TOOLS), so this field is currently unread by anything
            # that reaches here. Kept anyway (lead-coder review): the
            # danger is a FUTURE session_state_dir-reading tool being
            # dispatched through this SAME path with the field silently
            # missing again — the field costs one getattr; a future
            # session_state_dir-sensitive tool dispatched here with it
            # unset would repeat this exact incident. Do not remove
            # merely because nothing reads it today.
            session_state_dir=getattr(host, "session_state_dir", None),
        )
        # #3546: the driver-session's LIVE contextual narrowing — read off the
        # bound Session (public accessor), NOT off ``host``, whose own
        # ``contextual_permission`` is the raw value frozen at construction
        # (``Session._build_router_waist``) and therefore predates the spawn-time
        # ``apply_per_session_narrowing`` that installs the inherited narrowing.
        # ``bind_session`` sets ``_session`` and ``_router_host`` together, so the
        # host guard above already covers this read.
        return _make_tool_dispatch(
            ctx, contextual_permission=self._session.contextual_permission,
        )

    async def _finish(
        self, *, status: str, output: Any = None, error: "str | None" = None,
        named_stores: "dict | None" = None,
    ) -> None:
        """Deliver the result to the reply address (when ``notify_reply``), THEN
        write the terminal marker, THEN fire ``task_settled`` (settle-path,
        #3978), then arm the standard ephemeral vanish for this session (A10:
        the driver-session must not leak past terminal).

        IS-6: on the sync ATTACHED path ``notify_reply`` is False — the attached
        caller reads the terminal marker in-band via ``read_result``, so posting
        a ``pipeline_result`` turn to that same session would be a redundant,
        unprompted extra LLM turn. The terminal marker is ALWAYS written
        (``delivered=False`` records "no inbox delivery attempted"); only the
        cross-session reply is gated."""
        from reyn.core.pipeline.work_order import write_result

        delivered = (
            await self._deliver(status=status, output=output, error=error)
            if self._notify_reply else False
        )
        write_result(
            self._run_dir(), status=status, delivered=delivered,
            output=_json_safe(output), error=error,
            named_stores=_json_safe(named_stores) if named_stores is not None else None,
        )
        # proposal 0067 settle path (#3978): task_settled fires on the FACT
        # of settling — independent of whether delivery succeeded (ADR-0040
        # D4④, architect ruling "B", adopted 2026-08-10). Decisive reason,
        # not "separately"'s wording alone: §4-b's Composer `all` combinator
        # waits for every one of N tasks to settle; if task_settled were
        # gated on delivery success, a single on_settle="drop" (or a
        # vanished-reply-target fail-safe) would mean that task NEVER fires
        # its settle event, and `all` would wait forever — composition rests
        # on "settled", not on "delivered". Dispatched through THIS
        # driver-session (`self._session`, always live at this point in the
        # run), not the possibly-unresolvable reply `target` — the payload's
        # own `session` field already records WHICH session the settle
        # concerns; the DISPATCH surface does not need to be that session.
        # kind="pipeline" is a placeholder value (P4 has not landed the
        # prompt|pipeline|exec vocabulary yet).
        if self._notify_reply and self._session is not None:
            await self._session.dispatch_external_event(
                "task_settled",
                build_hook_payload(
                    "task_settled", task_id=self._work_order.run_id, kind="pipeline",
                    status=status, session=self._work_order.reply_to_sid or "main",
                    result=self._format_result_text(status=status, output=output, error=error),
                ),
            )
        # Reuse the existing ephemeral auto-vanish teardown (quiesce + cancel
        # run-loop + drop + session_vanished + per-session dir purge) instead of
        # a second teardown path. Same public seam spawn_session_recorded uses
        # (#5336: Session.mark_ephemeral() — was a private-attribute poke).
        if self._session is not None:
            self._session.mark_ephemeral()

    async def _deliver(
        self, *, status: str, output: Any, error: "str | None",
    ) -> bool:
        """Post the ``pipeline_result`` to the reply (agent, sid) via the
        settle path (proposal 0067 § "the settle path"): resolve the reply
        target (``resolve_reply_target`` — fail-safe: a vanished reply
        target is LOGGED and dropped, never rerouted to main, and the run
        still goes terminal so it cannot re-wake forever), then execute
        this run's ``on_settle`` disposition through the reply session's
        OWN ``ChainManager.settle()`` — the same pop+cancel_timeout+journal
        operation ``delegate_to_agent``'s chain-resolve already uses
        (D4: "immediate deletion", same function as delivery, not a
        separate later step). ``task_settled`` is NOT dispatched here — see
        ``_finish``, which fires it independent of this method's outcome."""
        wo = self._work_order
        target, reason = await resolve_reply_target(
            self._registry, wo.reply_to_agent, wo.reply_to_sid,
        )
        if target is None:
            logger.warning(
                "pipeline_result for run %r dropped: %s "
                "(fail-safe — NOT rerouted to main)", wo.run_id, reason,
            )
            # Proposal 0067 P9 (#3978), architect ruling 2026-08-10: a durable
            # record of this ALREADY-EXISTING drop, alongside the log line
            # above — not a new delivery path (the drop behavior itself is
            # unchanged; lead-coder's framing: "follow each producer's
            # existing default, don't invent a new destination"). Best-effort
            # (self._session may be None — should not happen at this point in
            # the driver's own lifecycle, but this emit must never be why a
            # settle fails): the run still goes terminal regardless.
            if self._session is not None:
                self._session._audit_events.emit(  # noqa: SLF001 — same-module driver/session pairing
                    "task_settle_undelivered",
                    run_id=wo.run_id, reply_to_agent=wo.reply_to_agent,
                    reply_to_sid=wo.reply_to_sid, reason=reason,
                )
            return False
        text = self._format_result_text(status=status, output=output, error=error)

        async def _post() -> None:
            await target.submit_pipeline_result(
                run_id=wo.run_id, pipeline_name=wo.pipeline_name, status=status,
                text=text, chain_id=new_chain_id(),
            )

        await target.chains.settle(wo.run_id, on_settle=wo.on_settle, deliver=_post)
        return True

    def _format_result_text(
        self, *, status: str, output: Any, error: "str | None",
    ) -> str:
        """The OS-framed message the reply session's LLM turn sees (the
        ``agent_response`` mirror: trusted OS framing + the payload as data)."""
        wo = self._work_order
        head = (
            f"[pipeline] run {wo.run_id} (pipeline {wo.pipeline_name!r}) "
            f"finished: status={status}"
        )
        if status == "ok":
            return f"{head}\nOutput:\n{json.dumps(_json_safe(output), ensure_ascii=False)}"
        return f"{head}\nError: {error}"


def _json_safe(value: Any) -> Any:
    """Best-effort JSON projection of a step result (steps normally return
    JSON-shaped values; anything else is stringified rather than crashing the
    terminal write)."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


__all__ = ["PipelineExecutorDriver", "MAX_RESUME_ATTEMPTS"]
