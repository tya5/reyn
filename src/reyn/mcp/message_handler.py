"""ReynMCPMessageHandler — server->client notifications bridge (#2597 S2b).

S2a (``MCPConnectionService``) holds one ``fastmcp.Client`` open per server for the
whole session lifetime. Because the connection stays open, FastMCP's ``session_task``
keeps its receive loop running — so server-pushed notifications (``tools/list_changed``,
``prompts/list_changed``, ``notifications/progress``) ARRIVE on the wire, but nothing
consumed them before S2b. This module installs the consumer.

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
    ``EventLog`` (#2597 S2b). One instance per held server connection.

    Scope (S2b): ``tools/list_changed``, ``prompts/list_changed``. ``resources/
    updated`` is bridged by slice ②b (see :meth:`on_resource_updated`) now that
    :class:`~reyn.mcp.connection_service.MCPConnectionService` actually tracks
    subscribed URIs — S2b itself deferred it because nothing was subscribed yet.
    ``on_resource_list_changed`` stays a base-class no-op (out of ②b's scope —
    no reyn caller subscribes to the resource LIST changing, only individual
    resource content updates).

    #2597 F2 (live-verified, NOT emitted here — see :meth:`on_progress`):
    ``notifications/progress`` is NOT bridged to ``mcp_progress`` by this handler.
    A live probe (real fastmcp 3.4.2 stdio server + a held ``MCPConnectionService``
    connection + a per-call ``progress_callback``, both wired simultaneously)
    confirmed the SDK dual-delivers every in-call progress notification: FastMCP's
    ``mcp.shared.session.BaseSession`` receive loop invokes the per-call
    ``progress_callback`` registered for that request's ``progressToken`` via
    ``ClientSession.call_tool(progress_callback=...)`` (``op_runtime/mcp.py``'s
    ``_on_progress`` — richer context: carries the tool name) AND separately
    dispatches the SAME notification through the installed ``message_handler``
    (this class) — a 3-step ``progress`` tool call produced 3
    ``PER_CALL_progress_cb`` events AND 3 ``mcp_progress`` bridge events with
    identical progress/total/message payloads. Emitting from BOTH paths would
    double every in-call progress event on the EventLog (mirrors the S2b-log
    dual-delivery already documented for LOGGING notifications). Since the
    per-call callback path already covers ALL call-scoped progress with richer
    context (the tool name; ``on_progress`` here can't see it — the bridge has no
    visibility into which in-flight request a ``progressToken`` belongs to), the
    minimal correct fix is: this bridge does not emit ``mcp_progress`` at all.
    Unsolicited/out-of-band progress (a notification with no per-call handler,
    e.g. a long-running server-initiated task with no corresponding client
    request) is out of scope until a real case demonstrates the SDK delivering
    one ONLY through the message_handler path — nothing observed here proves
    that path exists independently of the per-call one.
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

    async def on_resource_updated(self, message: Any) -> None:
        # #2597 slice ②b: the async push event-source this bridge exists for. The
        # notification carries ONLY the uri (MCP's resources/subscribe model is a
        # thin "something changed, re-read if you care" signal — see
        # reyn.mcp.client.MCPClient.subscribe_resource's docstring), so that's all
        # this event needs to carry too; a caller that wants the new content reads
        # the resource again. EventLog-only — deliberately NOT wired into the hook
        # dispatcher here (that's a future hooks-arc slice, per ②b's scope note).
        uri = getattr(getattr(message, "params", None), "uri", None)
        self._emit("mcp_resource_updated", server=self._server_name, uri=str(uri) if uri is not None else None)
        await super().on_resource_updated(message)

    async def on_progress(self, message: Any) -> None:
        # #2597 F2: deliberately does NOT emit ``mcp_progress`` — see this class's
        # docstring for the live-verified dual-delivery observation + the decision.
        # The per-call ``progress_callback`` path (``op_runtime/mcp.py``'s
        # ``_on_progress``) already emits ``mcp_progress`` (with tool-name context)
        # for every call-scoped progress notification the SDK also routes here.
        await super().on_progress(message)

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
