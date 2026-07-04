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

One nudge = drive the run to a terminal:

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
  work_order module docstring states the full contract).
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
permission_resolver / resolver / state_log — the session's narrowed
permission context, ⊆ the invoker's since the driver-session is spawned under
the invoker's identity). #2567: ``router_state`` is a real
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
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.core.events.state_log import StateLog
    from reyn.core.pipeline.work_order import PipelineWorkOrder
    from reyn.runtime.registry import AgentRegistry

logger = logging.getLogger(__name__)

# A8: recovery re-wakes past this count terminal-fail instead of resuming.
MAX_RESUME_ATTEMPTS = 3


class PipelineExecutorDriver:
    """ExecutionDriver that runs/resumes ONE pipeline work-order per nudge."""

    def __init__(
        self,
        work_order: "PipelineWorkOrder",
        *,
        registry: "AgentRegistry",
        state_log: "StateLog",
    ) -> None:
        self._work_order = work_order
        self._registry = registry
        self._state_log = state_log
        self._cancel_requested = False
        self._session: Any = None
        self._router_host: Any = None

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
        from reyn.core.pipeline.executor import PipelineExecutionError, PipelineExecutor
        from reyn.core.pipeline.serde import pipeline_from_dict
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
        executor = PipelineExecutor()
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
                )
            else:
                result = await executor.resume(
                    wo.run_id,
                    pipeline=pipeline,
                    tool_dispatch=await self._make_dispatch(),
                    state_log=self._state_log,
                    registry=self._registry,
                    default_identity=wo.reply_to_agent,
                )
        except PipelineExecutionError as exc:
            await self._finish(status="failed", error=str(exc))
            return
        await self._finish(status="ok", output=result.pipe_data)

    def is_cancel_requested(self) -> bool:
        """Cooperative cancel flag (protocol conformance; the executor has no
        mid-step cancel hook yet, so this only reports the request)."""
        return self._cancel_requested

    def request_cancel(self) -> None:
        """Record a cancel request (see ``is_cancel_requested``)."""
        self._cancel_requested = True

    async def _check_cap(self, user_text: str) -> None:
        """No-op: the router invocation cap has no meaning for a deterministic
        pipeline turn (the executor's own step list is the bound)."""
        return None

    # ── internals ──────────────────────────────────────────────────────────────

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
        )
        return _make_tool_dispatch(ctx)

    async def _finish(
        self, *, status: str, output: Any = None, error: "str | None" = None,
    ) -> None:
        """Deliver the result to the reply address, THEN write the terminal
        marker, then arm the standard ephemeral vanish for this session (A10:
        the driver-session must not leak past terminal)."""
        from reyn.core.pipeline.work_order import write_result

        delivered = await self._deliver(status=status, output=output, error=error)
        write_result(
            self._run_dir(), status=status, delivered=delivered,
            output=_json_safe(output), error=error,
        )
        # Reuse the existing ephemeral auto-vanish teardown (quiesce + cancel
        # run-loop + drop + session_vanished + per-session dir purge) instead of
        # a second teardown path. Same private poke spawn_session_recorded uses.
        if self._session is not None:
            self._session._ephemeral = True

    async def _deliver(
        self, *, status: str, output: Any, error: "str | None",
    ) -> bool:
        """Post the ``pipeline_result`` to the reply (agent, sid). Mirrors
        ``Session._a2a_send_response``'s routing (non-main sid → the specific
        live session; main → cold-load + ensure_running) including its
        fail-safe: a vanished reply target is LOGGED and dropped (never
        rerouted to main), and the run still goes terminal (``delivered:
        false`` in result.json) so it cannot re-wake forever."""
        wo = self._work_order
        registry = self._registry
        if not registry.exists(wo.reply_to_agent):
            logger.warning(
                "pipeline_result for run %r dropped: reply agent %r no longer exists",
                wo.run_id, wo.reply_to_agent,
            )
            return False
        if wo.reply_to_sid and wo.reply_to_sid != "main":
            target = registry.get_session(wo.reply_to_agent, wo.reply_to_sid)
            if target is None:
                logger.warning(
                    "pipeline_result for run %r dropped: reply session (%r, %r) is "
                    "no longer loaded (fail-safe — NOT rerouted to main)",
                    wo.run_id, wo.reply_to_agent, wo.reply_to_sid,
                )
                return False
            registry.ensure_session_running(wo.reply_to_agent, wo.reply_to_sid)
        else:
            target = registry.get_or_load(wo.reply_to_agent)
            await registry.ensure_running(wo.reply_to_agent)
        text = self._format_result_text(status=status, output=output, error=error)
        await target.submit_pipeline_result(
            run_id=wo.run_id, pipeline_name=wo.pipeline_name, status=status,
            text=text, chain_id=uuid.uuid4().hex,
        )
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
