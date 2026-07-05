"""ReynMCPMessageHandler — server->client notifications bridge (#2597 S2b / S2b-log).

S2a (``MCPConnectionService``) holds one ``fastmcp.Client`` open per server for the
whole session lifetime. Because the connection stays open, FastMCP's ``session_task``
keeps its receive loop running — so server-pushed notifications (``tools/list_changed``,
``prompts/list_changed``, ``notifications/progress``, ``notifications/message``
logging) ARRIVE on the wire, but nothing consumed them before S2b. This module installs
the consumer.

S2b-log routing (verified against the installed ``fastmcp`` 3.4.2 / ``mcp`` SDK source
— a live experiment against a real log-emitting FastMCP server, not guessed): MCP
logging (``notifications/message`` / ``LoggingMessageNotification``) reaches a held
client via TWO independent paths that both fire for the SAME wire message —
``mcp.client.session.ClientSession._received_notification`` routes it to the
Client-level ``log_handler`` kwarg (FastMCP's own ``default_log_handler`` if none is
given), AND ``shared.session.BaseSession._handle_incoming`` separately calls the
``message_handler`` (our ``ReynMCPMessageHandler``, via the inherited
``MessageHandler.dispatch`` match/case) — both call sites run unconditionally for every
notification (``shared/session.py``'s receive loop calls
``_received_notification(...)`` THEN ``_handle_incoming(...)``, not either/or). Since
``ReynMCPMessageHandler`` is already installed as the ``message_handler`` on every held
connection (S2b), the correct wiring is to add an ``on_logging_message`` hook to THIS
class (mirroring the existing hooks) rather than also wiring the separate
``log_handler`` kwarg — the latter would double-emit the same notification through a
second, redundant code path. No ``logging/setLevel`` is required to receive logs: the
experiment server emitted at its default level with no client-side ``setLevel`` call
and the notification arrived normally.

Design (verified against the installed ``fastmcp`` 3.4.2 source — see S2-pre spike):

  - **Base class**: :class:`fastmcp.client.tasks.TaskNotificationHandler`, NOT the bare
    ``fastmcp.client.messages.MessageHandler``. ``TaskNotificationHandler`` is FastMCP's
    own SEP-1686 task-status router — its ``dispatch()`` peeks every incoming
    ``ServerNotification`` for a ``TaskStatusNotification`` and forwards it to the owning
    ``Client`` BEFORE calling ``super().dispatch()`` (the base ``MessageHandler`` match/case
    that invokes ``on_tool_list_changed`` / ``on_prompt_list_changed`` / ``on_progress``
    etc). Subclassing (and NOT overriding ``dispatch`` itself) means task-status routing
    keeps running unconditionally on every message — our hook overrides only add behavior
    on top, they never replace or skip it.

  - **Two-phase client binding**: ``TaskNotificationHandler.__init__(self, client)``
    requires an already-constructed ``fastmcp.Client`` (it stores ``weakref.ref(client)``
    for the task-status routing above) — but FastMCP's own
    ``Client(transport, message_handler=...)`` constructor takes the handler as an
    argument, so the handler must exist BEFORE the ``Client`` object does. There is no
    factory hook for this in the public API (verified: ``fastmcp.client.client.Client``
    stores whatever ``message_handler`` object it's given verbatim in
    ``_session_kwargs["message_handler"]``, consumed later at ``__aenter__``/session-connect
    time — the client's *own* default path sidesteps this by constructing
    ``TaskNotificationHandler(self)`` INLINE inside its own ``__init__``, where ``self``
    already exists). This class breaks the same chicken-and-egg cycle explicitly:
    ``__init__`` skips ``TaskNotificationHandler.__init__`` (calls
    ``MessageHandler.__init__`` instead, which takes no arguments) and leaves
    ``_client_ref`` pointing at a ``lambda: None``; :meth:`bind_client` — called by
    :class:`~reyn.mcp.client.MCPClient` immediately after constructing the
    ``fastmcp.Client`` and before ``__aenter__()`` opens the transport — completes the
    weakref binding. No message can be dispatched before ``__aenter__()`` completes the
    handshake, so ``bind_client`` always runs before the first ``dispatch()`` call.

  - **Synchronous hook bodies**: the handler runs on FastMCP's ``session_task`` (not the
    agent turn task), but ``EventLog.emit()`` is fully synchronous and asyncio is
    single-loop with no preemption mid-sync-call, so calling ``emit_sink(...)`` directly
    from a hook is safe — no marshalling, no ``call_soon``, no queue (verified against the
    WAL's lock-free design — see S2-pre spike / connection_service.py's Option C
    docstring). Each hook below calls the sink SYNCHRONOUSLY (never ``await``s it) so a
    sink fault or a slow sink can never stall the receive loop or delay task-status
    routing; a fault in the sink is caught and swallowed (logged) rather than propagated,
    since notification handling must never crash the held connection's receive loop. The
    sole ``await super().<hook>(...)`` call in every override resolves against the base
    ``MessageHandler``'s bare ``pass`` body — no real async work, no scheduler yield of any
    consequence — so overriding these ``async def`` hooks (the base signature requires
    ``async def``; that is a FastMCP interface constraint, not a body-level await) does not
    reintroduce blocking.
"""
from __future__ import annotations

import logging
import weakref
from typing import Any, Callable

from fastmcp.client.messages import MessageHandler
from fastmcp.client.tasks import TaskNotificationHandler

logger = logging.getLogger(__name__)

# Matches EventLog.emit(type: str, **data) -> Event; a plain callable sink lets callers
# (session.py) defer resolution of a not-yet-constructed EventLog via a closure — see
# MCPConnectionService's emit_sink wiring.
EmitSink = Callable[..., Any]
ToolsCacheInvalidate = Callable[[str], None]


class ReynMCPMessageHandler(TaskNotificationHandler):
    """Bridges FastMCP server-pushed notifications on a held connection to reyn's
    ``EventLog`` (#2597 S2b / S2b-log). One instance per held server connection.

    Scope (S2b + S2b-log): ``tools/list_changed``, ``prompts/list_changed``,
    ``notifications/progress``, ``notifications/message`` (logging). ``resources/
    updated`` / ``resources/subscribe`` are DEFERRED to the resources-consumption slice
    (nothing is subscribed yet) — the inherited ``on_resource_updated`` /
    ``on_resource_list_changed`` stay base-class no-ops.
    """

    def __init__(
        self,
        emit_sink: EmitSink,
        server_name: str,
        *,
        tools_cache_invalidate: ToolsCacheInvalidate | None = None,
    ) -> None:
        # Deliberately bypass TaskNotificationHandler.__init__ — see module docstring
        # ("two-phase client binding"). MessageHandler.__init__ takes no arguments.
        MessageHandler.__init__(self)
        self._client_ref: Callable[[], Any] = lambda: None
        self._emit_sink = emit_sink
        self._server_name = server_name
        self._tools_cache_invalidate = tools_cache_invalidate

    def bind_client(self, client: Any) -> None:
        """Complete the ``TaskNotificationHandler`` weakref binding once the owning
        ``fastmcp.Client`` exists. MUST run before the first message is dispatched —
        :meth:`reyn.mcp.client.MCPClient.initialize` calls this immediately after
        constructing the ``fastmcp.Client`` and before ``__aenter__()`` opens the
        transport (= before any notification can possibly arrive)."""
        self._client_ref = weakref.ref(client)

    # ── notification hooks — synchronous bodies, see module docstring ──────────────

    async def on_tool_list_changed(self, message: Any) -> None:
        if self._tools_cache_invalidate is not None:
            try:
                self._tools_cache_invalidate(self._server_name)
            except Exception:  # noqa: BLE001 — a cache-invalidation fault must not drop the notification
                logger.warning(
                    "ReynMCPMessageHandler: tools_cache_invalidate failed for %r",
                    self._server_name, exc_info=True,
                )
        self._emit("mcp_tool_list_changed", server=self._server_name)
        await super().on_tool_list_changed(message)

    async def on_prompt_list_changed(self, message: Any) -> None:
        self._emit("mcp_prompt_list_changed", server=self._server_name)
        await super().on_prompt_list_changed(message)

    async def on_progress(self, message: Any) -> None:
        params = getattr(message, "params", None)
        token = getattr(params, "progressToken", None)
        self._emit(
            "mcp_progress",
            server=self._server_name,
            tool=None,
            progress=getattr(params, "progress", None),
            total=getattr(params, "total", None),
            message=getattr(params, "message", None),
            progress_token=str(token) if token is not None else None,
        )
        await super().on_progress(message)

    async def on_logging_message(self, message: Any) -> None:
        params = getattr(message, "params", None)
        self._emit(
            "mcp_log",
            server=self._server_name,
            level=getattr(params, "level", None),
            logger=getattr(params, "logger", None),
            data=getattr(params, "data", None),
        )
        await super().on_logging_message(message)

    # ── sink dispatch ───────────────────────────────────────────────────────────────

    def _emit(self, event_type: str, **data: Any) -> None:
        """Call the emit sink SYNCHRONOUSLY (never awaited — see module docstring).
        Never raises: a fault in the sink must not break notification dispatch on the
        held connection's receive loop."""
        try:
            self._emit_sink(event_type, **data)
        except Exception:  # noqa: BLE001 — sink faults must not break the receive loop
            logger.warning(
                "ReynMCPMessageHandler: emit_sink failed for %r on server %r",
                event_type, self._server_name, exc_info=True,
            )
