"""ReynMCPMessageHandler — server->client notifications bridge (#2597 S2b).

S2a (``MCPConnectionService``) holds one ``mcp.Client`` open per server for the whole
session lifetime (was ``fastmcp.Client`` pre-#4282; fastmcp is retired from the client
path, this module now binds to the official SDK's own ``Client``). Because the
connection stays open, the client's own session task keeps its receive loop running —
so server-pushed notifications (``tools/list_changed``, ``prompts/list_changed``,
``notifications/progress``) ARRIVE on the wire, but nothing consumed them before S2b.
This module installs the consumer.

## Composition, not inheritance (#3698 P3)

Earlier versions of this class subclassed ``fastmcp.client.tasks.TaskNotificationHandler``
(itself a subclass of ``fastmcp.client.messages.MessageHandler``). #3698 P3 measured
the ACTUAL call contract by reading the installed ``mcp``/``fastmcp`` source directly:
``mcp.client.session.ClientSession`` invokes ``self._message_handler(req)`` — a plain
``Callable`` (``MessageHandlerFnT``, a ``Protocol``, not a class requirement) —
``fastmcp.client.client.Client.__init__`` just stores whatever ``message_handler=``
object it is given, verbatim. **No inheritance is required anywhere in the real call
chain.** This class now implements :meth:`__call__` directly instead.

What the inheritance used to provide, and how this class now provides it itself:

  - **Task-status routing** (``TaskNotificationHandler.dispatch``'s own job): peek every
    incoming ``ServerNotification`` for a ``TaskStatusNotification`` and forward it to
    the owning ``Client`` via the weakref binding below — :meth:`__call__` does this
    directly now, in the same ``match`` statement as the rest of the routing (see
    below), rather than via ``super().dispatch()``.
  - **Message-type routing** (``MessageHandler.dispatch``'s own job): the base class
    matched all 14 message shapes (requests, 7 notification subtypes, exceptions) and
    routed each to its own ``on_X`` hook, all defaulting to a no-op ``pass`` unless
    overridden. This class only ever overrode 4 of those 14 (``on_tool_list_changed``/
    ``on_prompt_list_changed``/``on_resource_updated``/``on_progress``) — the other 10
    were always inherited no-ops. :meth:`__call__`'s ``match`` now routes ONLY those 4
    shapes (plus task-status) directly; everything else calls :meth:`_log_unhandled`
    rather than silently reaching nothing (see below — this is a DELIBERATE behavior
    addition, not a byte-identical port).

  - **Two-phase client binding** (unchanged mechanism, no longer inherited; #4836
    restated for the official SDK — fastmcp is retired from the client path, #4282):
    the owning ``mcp.Client`` does not exist yet when this handler must be
    constructed — the official SDK's own ``Client(transport, message_handler=...)``
    takes the handler as a constructor argument (same contract fastmcp's ``Client``
    had), so the handler must exist first. The reason THIS class needed a two-phase
    bind rather than a single-shot constructor argument was never fastmcp-specific
    (that constraint transferred unchanged to the official SDK's ``Client`` — same
    "handler exists before client" ordering); what fastmcp genuinely provided that
    the official SDK does not is a matching ``_handle_task_status_notification``
    method (#4457: task-status notifications don't currently reach this handler at
    all on mcp 2.0, so that forwarding call is presently dead code either way) —
    :meth:`bind_client`, called by :class:`~reyn.mcp.client.MCPClient` immediately
    after constructing the ``mcp.Client`` and before ``__aenter__()`` opens the
    transport, completes a weakref binding (``self._client_ref``) that
    :meth:`__call__` reads for task-status forwarding. No message can be dispatched
    before ``__aenter__()`` completes the handshake, so ``bind_client`` always runs
    before the first ``__call__``.

  - **Synchronous hook bodies**: the handler runs on the client's own receive-loop task
    (the ``mcp`` SDK's ``ClientSession``/``BaseSession`` machinery — not the agent turn
    task), but ``EventLog.emit()`` is fully synchronous and asyncio is
    single-loop with no preemption mid-sync-call, so calling ``emit_sink(...)`` directly
    from a hook is safe — no marshalling, no ``call_soon``, no queue (verified against the
    WAL's lock-free design — see S2-pre spike / connection_service.py's Option C
    docstring). Each hook below calls the sink SYNCHRONOUSLY (never ``await``s it) so a
    sink fault or a slow sink can never stall the receive loop or delay task-status
    routing; a fault in the sink is caught and swallowed (logged) rather than propagated,
    since notification handling must never crash the held connection's receive loop.

## What actually changed vs. the inherited version (named explicitly, #3698 P3 review)

For every message shape the real MCP protocol produces TODAY, observable behavior is
identical: the 4 acted-on notification types still trigger the same hook bodies
(unchanged), task-status still routes to the client, and every other shape still
produces no ``EventLog``/hook-trigger side effect. **One deliberate addition**: an
unrecognized message shape now emits a log line (:meth:`_log_unhandled` /
:meth:`_log_unknown_shape`) instead of silently reaching an inherited no-op —
lead-coder's P3 review condition: a closed match-list must not silently drop an
"unknown to us" shape the same way it silently finishes handling a KNOWN one; the day
fastmcp/MCP adds a new notification type this class doesn't yet act on, that day is
now distinguishable in the log from an ordinary quiet run, rather than reading
identical to one.

**#4812 (classifier, not just a severity bump)**: the original single DEBUG line
covered two genuinely different populations under one severity — "a real,
protocol-compliant ``ServerNotification`` variant reyn has no behavior for" (still
DEBUG, :meth:`_log_unhandled`) and "a shape this handler cannot classify at all"
(now WARNING, :meth:`_log_unknown_shape`: a future SDK-added notification variant, a
non-conforming server, or a real transport-level ``Exception``). Every currently-known
``ServerNotification`` union member (per the installed SDK's own
``typing.get_args(mcp.types.ServerNotification)``) now has an explicit named ``case``
in :meth:`__call__` — see that method's own comments for the exhaustiveness this split
depends on, and for the (already-known, #4457-tracked) ``TaskStatusNotification`` case
that stays unreachable on the pinned SDK without being deleted.
"""
from __future__ import annotations

import logging
import weakref
from typing import Any, Callable

import mcp.types

logger = logging.getLogger(__name__)

# Matches EventLog.emit(type: str, **data) -> Event; a plain callable sink lets callers
# (session.py) defer resolution of a not-yet-constructed EventLog via a closure — see
# MCPConnectionService's emit_sink wiring.
EmitSink = Callable[..., Any]
ToolsCacheInvalidate = Callable[[str], None]
# #2608 H1 / Hook-Event Redesign #2875 F1 (proposal 0059 §6 completion): SYNCHRONOUS
# raw-signal callable (never awaited here — the handler runs on the receive-loop task,
# see module docstring's "synchronous hook bodies"). Carries the RAW MCP signal
# (uri, resync) — NOT a pre-built payload — so the payload-construction step (Phase 1's
# ``build_hook_payload``) happens exactly once, inside
# ``reyn.hooks.ingress.McpIngressAdapter.to_event`` (the adapter this handler's caller,
# ``MCPConnectionService``, binds this callback to per-server). Before #2875 this
# handler built the payload itself via ``build_hook_payload`` directly, bypassing
# ``McpIngressAdapter.to_event`` entirely — the 1 of 4 ingress sources (MCP/Fs/Cron/
# Webhook) not actually funnelling through its own adapter's ``to_event``, per the §6
# unify. None = no external-event hook bridge (byte-identical to pre-H1 behaviour — the
# ephemeral MCPClientPool path and any session with no ``mcp_resource_updated`` hook
# configured never wire this).
OnExternalEvent = Callable[[str | None, bool], None]


class ReynMCPMessageHandler:
    """Bridges MCP server-pushed notifications on a held connection to reyn's
    ``EventLog`` (#2597 S2b). One instance per held server connection.

    Composes, does not inherit, the underlying message-handling contract — see module
    docstring's "#3698 P3" section for the full design and what changed.

    Scope (S2b): ``tools/list_changed``, ``prompts/list_changed``. ``resources/
    updated`` is bridged by slice ②b (see :meth:`on_resource_updated`) now that
    :class:`~reyn.mcp.connection_service.MCPConnectionService` actually tracks
    subscribed URIs — S2b itself deferred it because nothing was subscribed yet.
    ``ResourceListChangedNotification`` stays unhandled (out of ②b's scope —
    no reyn caller subscribes to the resource LIST changing, only individual
    resource content updates) — it now reaches :meth:`_log_unhandled` rather than
    an inherited no-op, same as every other shape reyn doesn't act on.

    #2608 H1: ``on_resource_updated`` ALSO fires a user-configured
    ``mcp_resource_updated`` hook (external-event->hooks arc, first slice) via
    ``self._on_external_event`` — see that method for the sync->async bridge design.

    #2597 P1 (reconnect resync-read, follow-up to ②b): :meth:`emit_resource_updated`
    factors the real-push producer logic out so ``MCPConnectionService`` can also
    call it synthetically, once per re-subscribed URI, on a transport-death
    reconnect — see that method's docstring.

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
        on_external_event: "OnExternalEvent | None" = None,
        agent_name: str | None = None,
    ) -> None:
        self._client_ref: Callable[[], Any] = lambda: None
        self._emit_sink = emit_sink
        self._server_name = server_name
        self._tools_cache_invalidate = tools_cache_invalidate
        # #2608 H1: the bounded sync->async bridge into this session's HookDispatcher.
        # None = no bridge (no hook trigger wired — byte-identical to pre-H1).
        self._on_external_event = on_external_event
        self._agent_name = agent_name
        # #3698 PR-2 (review ruling): per-FAMILY legacy-dispatch suppression
        # while a ListenSubscriptionAdapter is active and HONORS that family
        # — see :meth:`set_listen_honored`/:meth:`clear_listen_honored` and
        # ``__call__``'s three gated ``case`` arms below. Deliberately does
        # NOT cover ``on_progress`` (request-scoped, never rides ``listen()``
        # — see that method's own docstring; blanket-disabling this whole
        # handler while listen is active would silently drop progress, the
        # exact "no-longer-silent" failure mode this handler's own module
        # docstring names as the thing to avoid). False by default: a
        # connection with no active listen adapter (legacy era, or before
        # the first successful ``open()``) dispatches every family via the
        # legacy channel exactly as before this ruling.
        self._listen_honors_tools = False
        self._listen_honors_prompts = False
        self._listen_honors_resources = False

    def bind_client(self, client: Any) -> None:
        """Complete the weakref binding once the owning ``mcp.Client`` (the
        official SDK's, since #4282/#4836 — was fastmcp's ``Client`` pre-#4282,
        same constructor-ordering contract either way) exists. MUST run before
        the first message is dispatched — see module docstring's "two-phase
        client binding"."""
        self._client_ref = weakref.ref(client)

    def is_bound(self) -> bool:
        """#4836: True once :meth:`bind_client` has run and its weakref target
        is still alive. The public surface a caller (or a test) checks for
        "has the two-phase binding completed" — reads the same
        ``self._client_ref`` :meth:`__call__` uses for task-status forwarding,
        without exposing that private attribute itself."""
        return self._client_ref() is not None

    def set_listen_honored(self, *, tools: bool, prompts: bool, resources: bool) -> None:
        """Called by :class:`~reyn.mcp.subscription_port.ListenSubscriptionAdapter`
        after every successful ``open()`` — records, per family, whether the
        server's ``subscriptions/listen`` ack actually honored it (``sub.
        honored.tools_list_changed``/``.prompts_list_changed`` — ``bool |
        None``, ``False`` here for both ``False`` AND ``None``/"unknown",
        which is deliberately the SAME as "not honored": if the server
        didn't confirm it's delivering this family over listen, the legacy
        channel must stay live for it, matching the review ruling's
        "honored が None なら何も抑止しない" — resources from ``sub.honored.
        resource_subscriptions is not None`` — a real, non-``None`` list,
        even if empty, means the server acknowledged the resource-
        subscriptions mechanism under listen for this open() call.

        A server that dual-fires BOTH the legacy notification and the
        modern listen event for an honored family (a real, plausible
        migration-period shape — not just reyn's own test doubles; see
        ``subscription_port.py``'s module docstring) would otherwise double
        every ``mcp_tool_list_changed``/``mcp_prompt_list_changed``/
        ``mcp_resource_updated`` audit-event AND double-fire the H1 hook
        trigger + (tools family) the lazy-tools-cache invalidation — a
        real, user-visible harm, not a cosmetic double-count (#3698 review:
        found live via a previously-green test that started failing once
        this port's listen adapter actually started dispatching)."""
        self._listen_honors_tools = tools
        self._listen_honors_prompts = prompts
        self._listen_honors_resources = resources

    def clear_listen_honored(self) -> None:
        """Called by :class:`~reyn.mcp.subscription_port.ListenSubscriptionAdapter`
        on :meth:`~reyn.mcp.subscription_port.ListenSubscriptionAdapter.close`
        — the legacy channel becomes authoritative again for every family
        once listen delivery is no longer active (a reconnect that lands on
        a legacy-era server, or teardown)."""
        self._listen_honors_tools = False
        self._listen_honors_prompts = False
        self._listen_honors_resources = False

    # ── the MCP SDK's actual entry point (see module docstring's "#3698 P3") ───────

    async def __call__(self, message: Any) -> None:
        """Route an incoming MCP message. Called directly by
        ``mcp.client.session.ClientSession`` as ``self._message_handler(req)`` — a
        plain ``Callable``, no base-class contract required (module docstring)."""
        if isinstance(message, mcp.types.ServerNotification):
            # #4412 pin-bump PR: on 1.x, ServerNotification was a RootModel
            # wrapper class and `.root` unwrapped it to the specific
            # notification. On 2.0, ServerNotification is a bare
            # `X | Y | Z` type alias (confirmed live:
            # `type(mcp.types.ServerNotification) is types.UnionType`) —
            # `message` (already narrowed by the isinstance check above) IS
            # the specific notification directly; there is no wrapper left
            # to unwrap.
            root = message
            match root:
                case mcp.types.TaskStatusNotification():
                    client = self._client_ref()
                    if client is not None:
                        client._handle_task_status_notification(root)
                case mcp.types.ToolListChangedNotification():
                    # #3698 PR-2 (review ruling): suppressed only while an
                    # active listen adapter HONORS this family — see
                    # set_listen_honored's docstring for why (a dual-firing
                    # server would otherwise double this event/hook/cache-
                    # invalidate).
                    if not self._listen_honors_tools:
                        await self.on_tool_list_changed(root)
                case mcp.types.PromptListChangedNotification():
                    if not self._listen_honors_prompts:
                        await self.on_prompt_list_changed(root)
                case mcp.types.ResourceUpdatedNotification():
                    if not self._listen_honors_resources:
                        await self.on_resource_updated(root)
                case mcp.types.ProgressNotification():
                    # Deliberately UNGATED — progress is request-scoped, never
                    # rides listen() (see on_progress's own docstring); a
                    # blanket suppression here would silently drop it, the
                    # exact class of bug this handler's module docstring
                    # exists to make un-silent.
                    await self.on_progress(root)
                # #4812: the remaining KNOWN-BUT-UNACTED-ON ServerNotification
                # union members, named explicitly (not left to the wildcard
                # below). Each is real, protocol-compliant traffic a
                # standards-conforming server can send during entirely
                # normal operation — reyn simply has no behavior for it, by
                # design, same as the 5 shapes matched above. Enumerating
                # them BY NAME is what makes the wildcard's own severity
                # meaningful: see `_log_unknown_shape`'s docstring.
                case mcp.types.LoggingMessageNotification():
                    self._log_unhandled(root)
                case mcp.types.ResourceListChangedNotification():
                    self._log_unhandled(root)
                case mcp.types.ElicitCompleteNotification():
                    self._log_unhandled(root)
                case mcp.types.SubscriptionsAcknowledgedNotification():
                    # Reachable only when NO live listen() stream is
                    # consuming this ack for the relevant subscription (the
                    # installed SDK's own `_on_notify` routes an
                    # in-flight-listen ack to that stream directly instead
                    # of here — see this handler's module docstring on
                    # listen-honored suppression for the analogous
                    # tools/prompts/resources gating).
                    self._log_unhandled(root)
                # `CancelledNotification` (also a ServerNotification member)
                # is EXCLUDED here, not silently missing: the installed
                # SDK's own dispatcher (`ClientSession._on_notify`)
                # intercepts and returns before ever calling
                # `message_handler` for it (verified live,
                # `.venv/lib/.../mcp/client/session.py`) — it structurally
                # cannot reach this match statement, so there is no case
                # arm for it to fall into.
                case _:
                    # A genuinely UNCLASSIFIED ServerNotification shape — every
                    # currently-known union member (per `typing.get_args
                    # (mcp.types.ServerNotification)`, the installed SDK pin)
                    # has its own named `case` above; reaching this arm means
                    # the connected server sent something outside that set —
                    # either a FUTURE mcp-SDK-added variant this match hasn't
                    # been updated for, or a non-conforming server. #4812:
                    # THIS is the "unexpected/never-seen message type" #4805's
                    # own discriminator (fires only on the defect, not on
                    # healthy paths) asks for — unlike the named cases above,
                    # nothing legitimate is expected to land here.
                    self._log_unknown_shape(root)
        else:
            # #4812 correction: the installed SDK's own `IncomingMessage`
            # TypeAlias (`mcp/client/session.py`) is
            # `ServerNotification | Exception` — NOT `ServerNotification |
            # RequestResponder` as this branch's comment used to claim.
            # `message_handler` never receives a request in this SDK
            # version (sampling/roots/elicitation requests route through
            # their OWN dedicated callbacks, not this one) — the prior
            # "RequestResponder" wording described an API shape that does
            # not exist on the pinned SDK. Since `isinstance(message,
            # mcp.types.ServerNotification)` was False to reach this
            # branch, `message` here is ALWAYS an Exception: a real
            # transport-level fault (`ClientSession._on_stream_exception`),
            # never "legitimate expected traffic" the way an unacted-on
            # notification variant is — #4805's own discriminator (fires
            # only on the defect, never on a healthy path) applies
            # directly, unlike the notification branch above.
            self._log_unknown_shape(message)

    def _log_unhandled(self, message: Any) -> None:
        """#3698 P3 review condition: an OBSERVABLE trace for a KNOWN
        ``ServerNotification`` variant this handler doesn't act on by design
        (the ``case`` arms in ``__call__`` that route here) — legitimate,
        protocol-compliant traffic a standards-conforming server sends during
        entirely normal operation. Stays at ``debug`` (#4805): raising it
        would false-alarm on healthy traffic from a server that simply uses a
        notification shape reyn doesn't act on, the opposite of the
        "defect-only" property #4805's severity-raise fix depends on.

        #4812 split this method's OLD, wider job in two: this half keeps the
        original "known, unacted-on, not a defect" case; the genuinely
        unclassified case (a future/unknown shape, or a transport Exception)
        now routes to :meth:`_log_unknown_shape` instead — see that method
        and ``__call__``'s own comments for the enumerated population this
        split rests on."""
        logger.debug(
            "ReynMCPMessageHandler: no handler for message type %s on server %r",
            type(message).__name__, self._server_name,
        )

    def _log_unknown_shape(self, message: Any) -> None:
        """#4812: an OBSERVABLE, ``warning``-level trace for a message this
        handler cannot classify as a known, legitimate shape — the population
        __call__ routes here is EITHER a ``ServerNotification`` outside every
        named ``case`` in the match statement above (a future SDK-added
        variant, or a non-conforming server — see the match's own ``case _:``
        comment for the exhaustiveness argument this rests on) OR a real
        transport-level ``Exception`` (the ``else`` branch — never "expected
        traffic" the way a known-but-unacted-on notification is).

        This is the design #4812 itself asked for (quoting the issue): "a way
        to split the two populations ... so only the [actual anomaly] could
        reasonably raise severity without false-alarming on normal MCP
        servers." Distinct from :meth:`_log_unhandled` (stays at ``debug`` —
        that method's own docstring covers the KNOWN, non-anomalous half)."""
        logger.warning(
            "ReynMCPMessageHandler: unclassified message type %s on server %r",
            type(message).__name__, self._server_name,
        )

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

    async def on_prompt_list_changed(self, message: Any) -> None:
        self._emit("mcp_prompt_list_changed", server=self._server_name)

    async def on_resource_updated(self, message: Any) -> None:
        # #2597 slice ②b: the async push event-source this bridge exists for. The
        # notification carries ONLY the uri (MCP's resources/subscribe model is a
        # thin "something changed, re-read if you care" signal — see
        # reyn.mcp.client.MCPClient.subscribe_resource's docstring), so that's all
        # this event needs to carry too; a caller that wants the new content reads
        # the resource again. EventLog emit stays the audit-trail write (unchanged).
        #
        # #2608 H1 / #2597 P1: the actual emit + hook-trigger work is factored into
        # :meth:`emit_resource_updated` — see its docstring — so P1's synthetic
        # reconnect-resync path (``MCPConnectionService._ensure_open``) can produce
        # the IDENTICAL event shape and hook fire as a real push, just with
        # ``resync=True``.
        uri = getattr(getattr(message, "params", None), "uri", None)
        uri_str = str(uri) if uri is not None else None
        self.emit_resource_updated(uri_str, resync=False)

    def emit_resource_updated(self, uri: str | None, *, resync: bool) -> None:
        """The shared ``mcp_resource_updated`` producer: EventLog emit +
        (#2608 H1) hook-trigger enqueue. Called from two places:

          - :meth:`on_resource_updated` (a REAL server push, ``resync=False``).
          - ``MCPConnectionService._ensure_open`` (#2597 P1: on a genuine
            transport-death RECONNECT, once per re-subscribed tracked URI,
            ``resync=True``) — a synthetic "may have changed while disconnected,
            re-read if you care" signal. reyn keeps NO resource content cache
            (Q4 spike decision — subscriptions are runtime-only, no baseline to
            diff against), so it cannot tell whether a specific resource
            actually changed during the down window; it conservatively
            re-signals EVERY re-subscribed URI rather than silently dropping a
            real update that happened while the connection was dead.

        Both call sites produce the SAME event TYPE (``mcp_resource_updated``)
        through the SAME two downstream paths (EventLog ``emit_sink`` + H1's
        ``_on_external_event`` bridge into the hook dispatcher), so an existing
        ``mcp_resource_updated`` consumer or hook fires unchanged either way —
        only the added ``resync`` field distinguishes a synthetic re-signal
        from a real push for anyone inspecting the payload. Never raises: a
        fault in either downstream path must not break the receive loop (real
        push) or the reconnect (synthetic path).

        Hook-Event Redesign #2875 F1 (proposal 0059 §6 completion): this method
        passes the RAW signal (``uri``, ``resync``) to ``_on_external_event`` —
        it does NOT call ``build_hook_payload`` itself. Payload construction
        happens exactly once, downstream, inside
        ``reyn.hooks.ingress.McpIngressAdapter.to_event`` (bound per-server by
        ``MCPConnectionService``, the sole production wirer of
        ``_on_external_event`` — see its ``_ensure_open``). Before #2875 this
        method built the payload inline via ``build_hook_payload`` directly,
        which meant ``McpIngressAdapter.to_event`` — despite existing since
        Phase 2 (#2872) — was never actually reached in production for MCP,
        the 1 of 4 ingress sources (MCP/Fs/Cron/Webhook) not funnelling
        through its own adapter's ``to_event``."""
        self._emit("mcp_resource_updated", server=self._server_name, uri=uri, resync=resync)
        if self._on_external_event is not None:
            try:
                self._on_external_event(uri, resync)
            except Exception:  # noqa: BLE001 — a trigger fault must not break the receive loop
                logger.warning(
                    "ReynMCPMessageHandler: on_external_event failed for %r on server %r",
                    "mcp_resource_updated", self._server_name, exc_info=True,
                )

    async def on_progress(self, message: Any) -> None:
        # #2597 F2: deliberately does NOT emit ``mcp_progress`` — see this class's
        # docstring for the live-verified dual-delivery observation + the decision.
        # The per-call ``progress_callback`` path (``op_runtime/mcp.py``'s
        # ``_on_progress``) already emits ``mcp_progress`` (with tool-name context)
        # for every call-scoped progress notification the SDK also routes here.
        pass

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
