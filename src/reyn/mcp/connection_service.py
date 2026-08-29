"""MCPConnectionService — per-session held-open MCP connections (#2597 S2a).

#2597 slice ③ (elicitation): every held connection installs an
``elicitation_handler`` (see :mod:`reyn.mcp.elicitation`), built fresh on
every open/reopen alongside the existing ``ReynMCPMessageHandler`` (mirrors
the S2b per-open handler-rebuild pattern). ``elicitation_bus``/
``elicitation_gate`` (both optional, mirroring ``hook_trigger``'s None-
default no-op pattern) are the SAME "fixed bus + per-call gate" split #2095's
shell-hook consent uses (``session.py`` wires ``consent_bus=
self.as_request_bus()`` / ``consent_gate=lambda: self._interventions.
has_active_listener()``) — see :meth:`_resolve_elicitation_bus`.

Option C from the S2-pre spike (owner-delegated, do not relitigate): a persistent MCP
connection lives as a service INSIDE the agent's own session — not a separate driver
session. The spike proved the key precondition: FastMCP holds its client session in a
dedicated ``asyncio.Task``, so a client opened in task A is safely ``call_tool``'d from
task B on the SAME event loop. That is what makes holding connections open across
unrelated chat turns (which may run in different asyncio Tasks) safe, unlike
:class:`~reyn.mcp.pool.MCPClientPool` (a359 P2), whose ``get()`` fails fast off its
owning task because ITS contract is per-turn/task-affine by design.

Replaces the per-call open->close model on the live (non-ephemeral) session MCP path:
the pool opened + closed a fresh ``MCPClient`` (subprocess/HTTP session) on every single
tool call, which is correct but wasteful — this service opens each configured server
ONCE and reuses it for the rest of the session's lifetime.

Pool-compatible surface: ``get(server, config, *, agent_id=None) -> MCPClient`` matches
:meth:`MCPClientPool.get` byte-for-byte, so :class:`~reyn.mcp.gateway.MCPGateway` (the
one seam every MCP op flows through) works UNCHANGED when constructed with
``MCPGateway(pool=connection_service, ...)`` — it never has to know which kind of pool
it was handed.

Reconnect-on-demand (S2a-level resilience — deliberately NOT S2b's background health
loop / ping): a subprocess death or HTTP disconnect mid-session does NOT flip
``MCPClient.is_initialized()`` or the underlying ``fastmcp.Client.is_connected()``
(verified empirically against the real echo test server's ``die`` tool — both stay
True after the transport is gone) — the only observable signal is an exception raised
on the NEXT use. So the held-connection handle catches that signal, discards + reopens
the dead connection so the NEXT call lands on a healthy transport — a dropped
connection must not permanently wedge the server for the rest of the session.

#2597 F1 fix (post-S1 over-catch): ``MCPClient`` wraps EVERY exception (transport
death, application-level protocol errors, capability-gate refusals) into some
``MCPError`` subclass — so catching bare ``MCPError`` here would reconnect a perfectly
healthy connection on a capability-gate refusal or an app-level error (e.g. an unknown
tool/resource), needlessly killing+respawning a live stdio subprocess. ``_heal`` below
catches ONLY :class:`~reyn.mcp.client.MCPTransportError` — the narrower subclass
``reyn.mcp.client`` raises (via its ``_is_transport_death`` predicate, verified against
fastmcp 3.4.2 + the mcp SDK) exclusively for genuine transport-death. A
``MCPCapabilityError`` (gate refusal) or a plain ``MCPError`` (app-level) propagates
WITHOUT touching the connection.

CRITICAL — the reconnect must NOT silently retry a side-effectful call (at-most-once):
post-S1 ``call_tool`` raises ``MCPError`` on ANY transport failure, including the
drop-AFTER-execution window (the server RAN the tool, then the connection dropped before
its response arrived). Auto-retrying the same call on the fresh connection would
RE-EXECUTE the tool — an at-most-once → at-least-once regression vs the pre-S2a per-call
pool (a duplicated ``create_issue`` / ``send_email`` / counter increment). So the two op
classes are healed differently:

  - :meth:`_HeldConnection.call_tool` — **reconnect-then-propagate**: on ``MCPError``,
    heal the connection (reopen) but RE-RAISE the original error. The call is NOT
    retried, so a tool is executed at most once. The first ``call_tool`` right after an
    idle drop fails once; the healed connection makes every subsequent call succeed
    (S2b's proactive ping loop will detect the drop BEFORE the next call, delivering
    transparent healing SAFELY — S2a does not trade correctness for that UX).
  - :meth:`_HeldConnection.list_tools` — **retry-once**: an idempotent read is safe to
    re-run on the fresh connection, so it heals transparently (no user-visible failure).

Either way the fault the caller ultimately sees is contained by the existing MCPGateway
boundary into an LLM-visible error result, same as the pre-S2a per-call path.

Runtime-only state (S2a scope note): held connections are NOT WAL-derived / recoverable
state — they are reconstructed fresh (lazy-connect) after any process restart, exactly
like the pool's per-call clients were. Nothing here writes to the WAL.

#2597 S2b: because the connection stays open, FastMCP's ``session_task`` keeps its
receive loop running, so server-pushed notifications (tools/prompts ``list_changed``,
``notifications/progress``) arrive on the wire even between calls. ``emit_sink`` /
``tools_cache_invalidate`` (both optional; None = no bridge, byte-identical to pre-S2b
behaviour) are threaded down to a per-server :class:`~reyn.mcp.message_handler.
ReynMCPMessageHandler` built fresh each time :meth:`_ensure_open` opens (or reopens, on
reconnect) a held client — see that module for the notification->EventLog bridge design.

#2597 slice ②b — resource subscriptions (Q4, decided, do not relitigate): the
subscribed-URI set is RUNTIME-ONLY, in-memory, per server, held on THIS service
(``self._subscriptions``) — never WAL'd. A subscription carries no data of its own (MCP's
resources/subscribe is a thin "something changed, re-read if you care" signal, not a
message queue — see ``reyn.mcp.client.MCPClient.subscribe_resource``'s docstring), so it
is fully re-establishable and matches the gen-store runtime-only-state invariant. The
consequence: a fresh session (post-restart) starts with NO subscriptions (same as a fresh
``MCPClient`` starts with none), and a RECONNECT within the same live session (the F1
transport-death path) must explicitly RE-ISSUE ``subscribe_resource`` for every URI
tracked for that server on the fresh client — a brand-new ``mcp.ClientSession`` has no
memory of what the OLD (now-dead) session's client subscribed to. :meth:`_ensure_open`
does this re-subscribe immediately after opening a NEW client (whether that is the very
first open, where the tracked set is empty and the loop is a no-op, or a reconnect, where
it is the whole point) — see that method's inline comment.

#2608 H1 — external-event->hooks bridge (the first slice of the external-event arc):
``hook_trigger`` (optional, mirrors ``emit_sink``'s None-default no-op pattern) is an
ASYNC callable ``(point, template_vars) -> Awaitable`` — in practice a closure over the
owning session's ``HookDispatcher.dispatch``. It is never called directly from the MCP
receive-loop task (:class:`~reyn.mcp.message_handler.ReynMCPMessageHandler` runs
SYNCHRONOUSLY there and cannot ``await`` it — see that module's docstring). Instead this
service exposes :meth:`enqueue_external_event` — a SYNCHRONOUS, non-blocking
``put_nowait`` onto a BOUNDED ``asyncio.Queue`` (``_HOOK_EVENT_QUEUE_MAXSIZE`` entries)
— and drains it with a single background task (:meth:`_drain_hook_events`) running on
THIS service's (= the session's) event loop, which is what actually ``await``s
``hook_trigger``. Two invariants this buys:

  - **The receive loop never blocks and never stalls** on a slow/stuck hook — enqueue is
    O(1) and non-blocking; on overflow (a burst of resource updates arriving faster than
    hooks can be dispatched) the newest event is DROPPED + logged, never queued
    unboundedly and never backpressured onto the receive loop. This is the same
    "never stall / never delay other notification routing" discipline the module
    docstring establishes for the synchronous EventLog emit.
  - **Per-session dispatcher identity holds naturally**: because ``MCPConnectionService``
    is constructed per-session (see ``session.py``), the ``hook_trigger`` closure it is
    given targets THAT session's own ``HookDispatcher`` — a resource update on session A's
    held connection can only ever fire session A's hooks.

The drain task is lazily created on first ``enqueue_external_event`` call (mirrors the
lazy client-open pattern elsewhere in this class) and cancelled in :meth:`aclose`.
``hook_trigger=None`` (the ephemeral ``MCPClientPool`` path, or any session that never
wires one) → :meth:`enqueue_external_event` and the whole queue/drain-task machinery
never activate — byte-identical to pre-H1 behaviour.

#2597 P1 — reconnect resync-read (follow-up to ②b, higher-priority now that H1 makes a
missed update a missed hook fire): ②b re-subscribes every tracked URI on a
transport-death reconnect (the loop in :meth:`_ensure_open`, described above), but a
resource that actually CHANGED while the connection was dead never produced a
``resources/updated`` push — that notification simply never arrived on the dead
transport, and the fresh ``mcp.ClientSession`` has no way to redeliver a notification it
never received. Q4 (S2-pre spike, decided, do not relitigate): reyn keeps NO resource
content cache — subscriptions are runtime-only (see ②b's docstring above), so there is
no baseline to diff the post-reconnect content against and no way to know WHICH tracked
URIs, if any, actually changed during the down window. The chosen trade-off:
conservatively treat every reconnect as an implicit "may have changed, re-read if you
care" signal for every re-subscribed URI, rather than silently dropping a real update. So
:meth:`_ensure_open` distinguishes the very first open for a server (``_ever_opened`` has
not seen it yet — nothing to resync, no synthetic emit) from a RE-open (``is_reopen`` —
the same server was already opened once before in this service's lifetime): on a
re-open, after each successful re-subscribe, it calls
:meth:`~reyn.mcp.message_handler.ReynMCPMessageHandler.emit_resource_updated` (the
producer factored out of ``on_resource_updated`` for exactly this reuse) with
``resync=True`` — the SAME event type, through the SAME emit_sink + H1 hook-trigger
path a real push uses, so EventLog subscribers and H1 hooks fire identically to a real
push. A possibly-spurious re-read on a (rare) reconnect is cheap; a silently dropped real
update is not.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.hooks.ingress import McpIngressAdapter
from reyn.mcp.client import MCPClient, MCPTransportError
from reyn.mcp.elicitation import DEFAULT_ELICITATION_TIMEOUT_SECONDS, build_elicitation_handler
from reyn.mcp.message_handler import EmitSink, ReynMCPMessageHandler, ToolsCacheInvalidate
from reyn.mcp.pool import describe_fault, is_real_control_flow
from reyn.mcp.subscription_port import (
    ListenSubscriptionAdapter,
    SubscriptionAdapter,
    select_subscription_adapter,
)

if TYPE_CHECKING:
    from reyn.user_intervention import RequestBus

logger = logging.getLogger(__name__)

# #2608 H1: bound on the sync->async external-event bridge queue. Small and fixed —
# a burst of resource-update pushes beyond this is dropped (+logged), never queued
# unboundedly. Not currently exposed as config (H1 scope: prove the trigger mechanism;
# tuning the bound is a follow-up if a real workload needs it).
_HOOK_EVENT_QUEUE_MAXSIZE = 32

HookTrigger = Callable[[str, dict], Awaitable[Any]]

# #2597 slice ③: same "bus + gate" split #2095's shell-hook consent uses
# (session.py wires ``consent_bus=self.as_request_bus()`` /
# ``consent_gate=lambda: self._interventions.has_active_listener()``) — a
# fixed bus REFERENCE plus a per-call GATE, so "is a human attached right
# now" is re-evaluated fresh on every elicitation, not frozen at connection-
# open time (a TUI can mount/unmount between one elicitation and the next).
ElicitationGate = Callable[[], bool]


class MCPConnectionService:
    """Holds one open :class:`MCPClient` per configured server for the service's
    lifetime (= the owning agent session's lifetime). See module docstring for the
    Option C rationale, the pool-compatible ``get()`` contract, and the
    reconnect-on-demand design.

    Usage (mirrors ``MCPClientPool``, but no ``async with`` is required to use it —
    only :meth:`aclose` needs to run, at session teardown)::

        service = MCPConnectionService()
        client = await service.get("srv", cfg, agent_id="reyn/host")
        await client.call_tool("read_file", {"path": "x"})
        ...  # later turns, later tasks: the SAME connection is reused
        await service.aclose()  # session teardown — closes every held connection
    """

    def __init__(
        self,
        *,
        emit_sink: EmitSink | None = None,
        tools_cache_invalidate: ToolsCacheInvalidate | None = None,
        hook_trigger: "HookTrigger | None" = None,
        elicitation_bus: "RequestBus | None" = None,
        elicitation_gate: "ElicitationGate | None" = None,
        agent_name: str | None = None,
    ) -> None:
        # #2597 S2b: threaded into a fresh ReynMCPMessageHandler per held server
        # connection (see _ensure_open). None (default) = no notifications bridge —
        # the ephemeral per-call MCPClientPool path never constructs this service with
        # a sink, so it stays byte-identical to pre-S2b behaviour.
        self._emit_sink = emit_sink
        self._tools_cache_invalidate = tools_cache_invalidate
        # #2608 H1: the async closure over the owning session's HookDispatcher.dispatch.
        # None = no external-event hook bridge — see module docstring's H1 section.
        self._hook_trigger = hook_trigger
        # #2597 slice ③: mirrors #2095's consent_bus/consent_gate split (see
        # ElicitationGate's field comment above). None/None (the default —
        # every call site that doesn't explicitly wire elicitation, including
        # every existing test that constructs this service directly) means
        # every held connection's elicitation_bus_resolver always returns
        # None, i.e. every elicitation auto-declines (headless) — byte-
        # identical to pre-③ behaviour (no elicitation_handler installed
        # would have meant no elicitation capability declared at all; a
        # resolver that always returns None still declares the capability
        # per D6, but a server that never gets asked because no test
        # exercises it sees no behaviour change).
        self._elicitation_bus = elicitation_bus
        self._elicitation_gate = elicitation_gate
        self._agent_name = agent_name
        # Hook-Event Redesign Phase 2 (proposal 0059 §6.1): the bounded
        # queue+drain-task in-process bridge now lives in the shared
        # ``McpIngressAdapter`` (``reyn.hooks.ingress``) — :meth:`enqueue_external_event`
        # below delegates to it instead of owning the queue/drain machinery
        # itself. Byte-identical behaviour (same maxsize, same drop+log on
        # overflow, same lazy drain-task creation); the mechanism is just
        # consolidated with ``FsWatcher``'s identical shape instead of
        # duplicated.
        self._mcp_ingress_adapter = McpIngressAdapter(
            hook_trigger=hook_trigger, maxsize=_HOOK_EVENT_QUEUE_MAXSIZE,
            # #5521: reuse this service's OWN emit_sink (above) for the
            # ingress bridge's drain-task-death observation — the same
            # None-tolerant sink, not a second one.
            emit_event=emit_sink,
        )
        self._clients: dict[str, MCPClient] = {}
        # #2597 slice ②b: runtime-only, in-memory, NO WAL (Q4 — see module docstring).
        # server name -> set of URIs currently subscribed on that server's held
        # connection. Populated by _HeldConnection.subscribe_resource on success,
        # discarded by unsubscribe_resource, and consulted by _ensure_open to
        # re-subscribe every tracked URI on a fresh client (first open: empty, no-op;
        # reconnect: the whole point).
        self._subscriptions: dict[str, set[str]] = {}
        # #4686: the ``honored`` value from the most recent ``adapter.open()``
        # for each server — a set (the honored subset the server actually
        # confirmed) or ``None`` (cannot report honored-ness: a Legacy
        # connection, or no successful open yet). Was previously computed
        # and discarded locally inside ``_ensure_open`` (used only to decide
        # which URIs get a synthetic resync fire on reopen); kept here too so
        # ``unhonored_uris`` can expose the requested-vs-honored distinction
        # without re-deriving it. Runtime-only, no WAL, same Q4 reasoning as
        # ``_subscriptions`` above.
        self._last_honored: "dict[str, set[str] | None]" = {}
        # #3698 PR-2: the active SubscriptionAdapter per server — rebuilt on
        # every (re)connect by _ensure_open (see subscription_port.py's own
        # module docstring for the full design). Read by _HeldConnection.
        # subscribe_resource/unsubscribe_resource to route an incremental
        # add/remove through whichever mechanism the CURRENT connection's
        # negotiated version actually uses.
        self._subscription_adapters: dict[str, SubscriptionAdapter] = {}
        # #3698 PR-2: fire-and-forget reconnect tasks spawned by
        # _on_subscription_lost (a ListenSubscriptionAdapter's background
        # consumer noticing SubscriptionLost) — kept referenced so they are
        # never garbage-collected mid-flight, and cancelled in aclose() so a
        # session teardown can never race a proactive reconnect.
        self._background_tasks: "set[asyncio.Task[None]]" = set()
        # #2597 P1 (reconnect resync-read): servers for which _ensure_open has
        # completed at least one successful open in THIS service's lifetime — the
        # boundary that distinguishes the very first open (nothing to resync yet,
        # no synthetic emit) from a RE-open after a transport-death drop (every
        # tracked subscription may have missed a real update while dead, so each
        # gets a synthetic mcp_resource_updated). See _ensure_open.
        self._ever_opened: set[str] = set()
        # One handle per server, cached so repeated get() calls for the same server
        # return the SAME object (connection-reuse identity) across the connection's
        # whole lifetime, including through a reconnect: the handle looks up the
        # live MCPClient by server name on every call rather than binding to one
        # MCPClient instance.
        self._handles: dict[str, "_HeldConnection"] = {}
        # Per-server lock so two concurrent first-use get() calls for the SAME server
        # (e.g. two chat turns racing on session startup) don't both open a client.
        self._locks: dict[str, asyncio.Lock] = {}

    def held_servers(self) -> list[str]:
        """Names of servers with a currently-open held connection. Read-only
        introspection for callers/tests — mirrors ``MCPClient.is_initialized()``'s
        public-surface pattern (never reach into ``_clients`` directly)."""
        return list(self._clients.keys())

    def _lock_for(self, server: str) -> asyncio.Lock:
        lock = self._locks.get(server)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[server] = lock
        return lock

    async def get(self, server: str, config: dict, *, agent_id: str | None = None) -> "MCPClient":
        """Return the held connection handle for ``server``, opening (and caching)
        it on first use. Pool-compatible signature — see module docstring.

        Unlike ``MCPClientPool.get()``, this is intentionally NOT task-affine: the
        spike proved a FastMCP client's session task makes cross-task use safe, so a
        held connection opened during one chat turn (one asyncio Task) is reused
        from a later turn running in a different Task without failing fast.
        """
        async with self._lock_for(server):
            await self._ensure_open(server, config, agent_id=agent_id)
            handle = self._handles.get(server)
            if handle is None:
                handle = _HeldConnection(self, server, config, agent_id)
                self._handles[server] = handle
            return handle  # type: ignore[return-value]  # duck-types MCPClient's call_tool/list_tools/is_initialized

    async def _ensure_open(
        self, server: str, config: dict, *, agent_id: str | None,
    ) -> MCPClient:
        """Return the live held client for ``server``, discarding + reopening a
        client that was explicitly closed out from under this service (defensive —
        the common dead-connection case is caught reactively by ``_HeldConnection``,
        not detected here; see module docstring)."""
        client = self._clients.get(server)
        if client is not None and not client.is_initialized():
            self._clients.pop(server, None)
            client = None
        if client is None:
            # #2597 P1: computed BEFORE this open completes — True iff a PRIOR open
            # for this server already succeeded in this service's lifetime, i.e.
            # this is a RE-open (reconnect after a transport-death drop), not the
            # very first open. See _ever_opened's field comment + the re-subscribe
            # loop below.
            is_reopen = server in self._ever_opened
            # #2597 S2b: a fresh handler per open (including every reconnect) — bound
            # to the server name closed over here, so a reconnected client's
            # notifications keep landing under the same server attribution.
            handler = None
            if self._emit_sink is not None:
                handler = ReynMCPMessageHandler(
                    self._emit_sink, server,
                    tools_cache_invalidate=self._tools_cache_invalidate,
                    # #2608 H1 / Hook-Event Redesign #2875 F1: wired only when a
                    # hook_trigger was injected (this service's ingress path is itself
                    # a no-op without one) — so a session with no hook_trigger stays
                    # byte-identical to pre-H1. Bound to THIS server name via
                    # functools.partial — :meth:`_mcp_to_hook_event` is per-service
                    # (one ``McpIngressAdapter``, shared across every held server),
                    # but the raw signal the handler sees (``uri``, ``resync``) does
                    # not itself carry which server produced it.
                    on_external_event=(
                        functools.partial(self._mcp_to_hook_event, server)
                        if self._hook_trigger is not None else None
                    ),
                    agent_name=self._agent_name,
                )
            # #2597 slice ③ D6: EVERY held connection installs an elicitation
            # handler (unconditionally — no "does this session have a bus"
            # branch here; that branch lives INSIDE the handler, see
            # reyn.mcp.elicitation's module docstring), so every held
            # connection always declares the ``elicitation`` client
            # capability. Per-server config overrides:
            #   - ``elicitation: "auto_decline"`` — always decline, even with
            #     a live listener (operator wants this server silenced).
            #   - ``elicitation_timeout_seconds`` — per-server deadline
            #     override (default DEFAULT_ELICITATION_TIMEOUT_SECONDS).
            elicitation_handler = build_elicitation_handler(
                server_name=server,
                bus_resolver=self._resolve_elicitation_bus,
                emit_sink=self._emit_sink,
                timeout_seconds=float(
                    config.get("elicitation_timeout_seconds")
                    or DEFAULT_ELICITATION_TIMEOUT_SECONDS
                ),
                mode=str(config.get("elicitation") or "prompt"),
            )
            client = MCPClient(
                config, agent_id=agent_id, message_handler=handler,
                elicitation_handler=elicitation_handler, server_name=server,
                # #3821: the sink reaches the client itself so an UNSANDBOXED
                # stdio fallback leaves an audit-event, not only a warning.
                # Same None-tolerant shape as every other use of _emit_sink here.
                emit_event=self._emit_sink,
            )
            await client.__aenter__()  # initialize; held open (no matching __aexit__ until aclose/reconnect)
            self._clients[server] = client
            self._ever_opened.add(server)
            # #3698 PR-2: build the adapter matching THIS client's actual
            # negotiated version (see subscription_port.py's own module
            # docstring for the full design) and open() it — UNCONDITIONALLY,
            # even when the tracked URI set is empty: under a modern
            # negotiation, tools_list_changed/prompts_list_changed delivery
            # itself REQUIRES an open listen() stream regardless of whether
            # any resource is subscribed, unlike the legacy path (where the
            # message_handler push covers those two families for free, no
            # subscribe call needed). Stored per-server so a later
            # incremental subscribe_resource/unsubscribe_resource
            # (_HeldConnection, below) can route through the SAME adapter
            # instance rather than rebuilding it.
            #
            # #2597 slice ②b (adapter-mediated since PR-2): open() re-issues
            # delivery for every URI tracked for THIS server on the fresh
            # client. On the very first open the tracked set is empty
            # (nothing to (re)establish yet, though the listen adapter still
            # opens its stream for list_changed — see above); on a reconnect
            # (this same branch runs because the dead client was already
            # popped from self._clients by _reconnect below) this is what
            # makes a subscription survive a transport-death reconnect — a
            # brand-new connection has no memory of what the OLD one
            # subscribed to.
            #
            # #2597 P1 (reconnect resync-read, follow-up to ②b): on a RE-open
            # (``is_reopen`` — NOT the very first open), reyn cannot know
            # whether any of these URIs actually changed during the
            # disconnect window (Q4: no content cache to diff against — see
            # message_handler.py's ``emit_resource_updated`` docstring), so
            # it conservatively fires a SYNTHETIC ``mcp_resource_updated`` for
            # each re-established URI — through the exact same emit_sink + H1
            # hook-trigger path a real push uses, so a missed-during-
            # disconnect update is never silently dropped. ``honored`` drives
            # WHICH URIs get the synthetic fire when the adapter can say
            # (the listen adapter's filter-level ack); when it can't (``None``
            # — the legacy adapter, or a listen adapter the server declined
            # resource_subscriptions on entirely), every TRACKED URI gets one
            # — deliberately not narrowed to "only URIs whose individual
            # re-subscribe call didn't raise" (the pre-PR-2 legacy-only
            # behavior): a resync is a cheap "may have changed, re-read if
            # you care" signal, and firing it for a URI whose own re-
            # subscribe state is uncertain is safer than silently assuming
            # it's fine. First open: the tracked set is empty, so nothing is
            # ever emitted — only a genuine re-open with tracked URIs
            # produces synthetic events.
            on_lost = (
                # #3698 review: ``client`` (THIS open's own client, bound now
                # while it's still THE current one) is threaded through so
                # ``_on_subscription_lost`` can tell, when it actually fires,
                # whether it's still talking about the CURRENT connection —
                # see that method's own docstring for why this matters (the
                # double-reconnect bug this closes).
                functools.partial(self._on_subscription_lost, server, config, agent_id, client)
                if handler is not None else None
            )
            adapter = select_subscription_adapter(client, handler, on_lost=on_lost)
            self._subscription_adapters[server] = adapter
            tracked = set(self._subscriptions.get(server, ()))
            try:
                honored = await adapter.open(tracked)
            except Exception:  # noqa: BLE001 — subscription delivery is best-effort; must not abort the connect
                logger.warning(
                    "MCPConnectionService: failed to open subscription delivery for %r "
                    "after (re)connect", server, exc_info=True,
                )
                honored = None
            self._last_honored[server] = honored
            if is_reopen and handler is not None:
                for uri in (honored if honored is not None else tracked):
                    handler.emit_resource_updated(uri, resync=True)
            # #2597 capability/version gate: observability seam. This is the first
            # point in the live (non-ephemeral) session path that HAS the emit_sink
            # (the ephemeral per-call MCPClientPool path never wires one — see class
            # docstring — so it stays silent, matching pre-#2597 behaviour there).
            # Fires once per (re)connect, including reconnects (a version/capability
            # renegotiation is itself worth a trace event, not just the first
            # connect). #3698 PR-2: now ALSO names the selected adapter's class —
            # the witness that adapter SELECTION (not just "both paths work in
            # isolation") is observable per-connection, the exact acceptance
            # criterion lead-coder named for this PR.
            if self._emit_sink is not None:
                self._emit_sink(
                    "mcp_initialized",
                    server=server,
                    negotiated_version=client.negotiated_version,
                    capabilities=client.advertised_capabilities(),
                    subscription_adapter=type(adapter).__name__,
                )
        return client

    async def _reconnect(
        self, server: str, config: dict, *, agent_id: str | None,
    ) -> MCPClient:
        """Discard the (dead) held client for ``server`` and open a fresh one.
        Teardown of the dead client is best-effort — its transport is already gone,
        so a teardown fault here is expected and never blocks the reconnect.

        #5280: if the reopen below (``_ensure_open``) itself raises — the
        server subprocess is genuinely gone/unreachable, not just a
        transient transport death — ``server`` has ALREADY been popped
        from ``self._clients`` (right above), so ``held_servers()`` (and
        therefore ``Session.mcp_subscription_state()``'s reactive cache,
        session.py) no longer lists it. None of the 6 kinds that cache
        subscribes to fire on a FAILED reopen — ``mcp_initialized`` only
        fires on success (see ``_ensure_open``'s own tail). Emits
        ``mcp_reconnect_failed`` here, the ONE place both call paths
        (``_HeldConnection._heal``'s reactive path and
        ``_reconnect_from_lost_subscription``'s proactive one) funnel
        through, so the cache invalidates on this path too instead of
        staying stale until an unrelated event happens to fire."""
        old = self._clients.pop(server, None)
        if old is not None:
            try:
                await old.__aexit__(None, None, None)
            except BaseException as exc:  # noqa: BLE001 — best-effort; the connection is already dead
                if is_real_control_flow(exc):
                    raise
                logger.warning(
                    "MCPConnectionService: teardown of dead connection %r contained: %r",
                    server, exc,
                )
        # #3698 PR-2: the OLD adapter (if any — first-ever connect has none)
        # is orphaned once _ensure_open below rebuilds a fresh one; close it
        # explicitly first so a ListenSubscriptionAdapter's background
        # consumer task never leaks across a reconnect. graceful=False (#3698
        # review ruling, live-verified): THIS caller already knows the
        # transport is dead — that's why _reconnect is running at all — so
        # there is no peer left to round-trip a graceful ``subscriptions/
        # listen`` close with. An earlier version called the graceful
        # close() unconditionally here and it hung indefinitely against a
        # known-dead transport; see subscription_port.py's module docstring
        # "Design record" #2a for the full finding and why bounding it with
        # a timeout was tried and rejected rather than fixed this way.
        old_adapter = self._subscription_adapters.pop(server, None)
        if old_adapter is not None:
            try:
                await old_adapter.close(graceful=False)
            except Exception:  # noqa: BLE001 — best-effort; the underlying connection is already dead
                logger.warning(
                    "MCPConnectionService: teardown of dead subscription adapter for %r "
                    "contained an error", server, exc_info=True,
                )
        try:
            return await self._ensure_open(server, config, agent_id=agent_id)
        except BaseException:
            # #5280 review (lead-coder, non-blocking): a bare ``except
            # Exception:`` here does not catch ``CancelledError`` (it's a
            # ``BaseException``, not an ``Exception``, since Python 3.8).
            # ``server`` is already popped from ``self._clients`` (above)
            # regardless of WHY the reopen didn't complete — a cancel
            # landing mid-``_ensure_open`` leaves the cache exactly as
            # stale as any other reopen failure would. ``raise``
            # unconditionally below re-raises the SAME exception
            # untouched (cancellation or otherwise) — this only adds the
            # emit as a side effect, never changes what propagates or
            # swallows a cancel (mirrors ``llm/llm.py``'s own ``except
            # BaseException`` precedent for "always look, never absorb").
            if self._emit_sink is not None:
                self._emit_sink("mcp_reconnect_failed", server=server)
            raise

    def _on_subscription_lost(
        self, server: str, config: dict, agent_id: "str | None", dead_client: "MCPClient",
    ) -> None:
        """The sync, non-blocking callback a :class:`~reyn.mcp.subscription_port.
        ListenSubscriptionAdapter` fires when its stream ends via
        ``SubscriptionLost`` (see that module for the full design) — mirrors
        #2608 H1's sync-callback-into-async-work bridge shape.

        Schedules a PROACTIVE reconnect: unlike ``call_tool``/``list_tools``
        (healed REACTIVELY, on the next use, via ``_HeldConnection._heal``),
        nothing "calls" to receive a subscription/list_changed notification
        — a purely reactive design would leave delivery silently dead until
        some UNRELATED client call happened to trigger a heal. This task is
        fire-and-forget (kept referenced in ``self._background_tasks`` so it
        is never garbage-collected mid-flight, and cancelled in
        :meth:`aclose`).

        ``dead_client`` (#3698 review, live-verified DOUBLE RECONNECT bug —
        this docstring PREVIOUSLY claimed "a genuinely concurrent client
        call racing this reconnect is not a NEW hazard... already
        tolerates" — that claim was WRONG, corrected here): the exact
        client instance THIS adapter belonged to, captured at the open()
        that installed it. A single transport death (e.g. the peer process
        dying) fires BOTH this callback (the listen stream noticing loss)
        AND, independently, ``_HeldConnection._heal``'s reactive path (an
        in-flight call failing with ``MCPTransportError``) — live-verified:
        one subprocess kill produced 3 ``mcp_initialized`` events (1 open +
        2 reconnects) and 2 synthetic ``mcp_resource_updated`` resyncs
        (expected 2 and 1). ``_reconnect_from_lost_subscription`` checks
        ``dead_client`` against the CURRENT held client, under the SAME
        per-server lock ``_HeldConnection._heal`` now also takes (see that
        method) — whichever path reconnects first wins; the second sees the
        client has already changed and skips, closing the race rather than
        merely narrowing it (a check without the shared lock would still
        race: both could pass a stale check before either replaces the
        client)."""
        task = asyncio.create_task(
            self._reconnect_from_lost_subscription(server, config, agent_id, dead_client)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _reconnect_from_lost_subscription(
        self, server: str, config: dict, agent_id: "str | None", dead_client: "MCPClient",
    ) -> None:
        try:
            async with self._lock_for(server):
                if self._clients.get(server) is not dead_client:
                    # Someone else (typically _HeldConnection._heal's own
                    # reactive path, racing the SAME transport death) already
                    # reconnected — see this method's docstring. Not stale
                    # data staying stale: we hold the lock, so this read is
                    # the current truth, not a snapshot that could still
                    # change under us.
                    return
                await self._reconnect(server, config, agent_id=agent_id)
        except Exception:  # noqa: BLE001 — a proactive reconnect attempt must never crash the service; the
            # next reactive _heal (a normal call_tool/list_tools) still retries.
            logger.warning(
                "MCPConnectionService: proactive reconnect after a lost subscription "
                "failed for %r", server, exc_info=True,
            )

    def subscribed_uris(self, server: str) -> list[str]:
        """Sorted list of URIs currently tracked as subscribed for ``server``.
        Read-only introspection for callers/tests (mirrors :meth:`held_servers`'s
        public-surface pattern) — never reach into ``_subscriptions`` directly.

        This is the REQUESTED set — the one reyn is trying to maintain — not
        the honored set. See :meth:`unhonored_uris` for which of these the
        server actually confirmed (#4686: the requested set stays the
        population on purpose, so a URI the server declined doesn't vanish
        from view — see that method's docstring)."""
        return sorted(self._subscriptions.get(server, ()))

    def subscription_mode(self, server: str) -> "str | None":
        """``"listen"`` or ``"legacy"`` — which :class:`SubscriptionAdapter`
        kind is currently active for ``server``, or ``None`` if no adapter
        has been built yet (no successful (re)connect). Read-only
        introspection (#4686), mirroring ``subscribed_uris``'s
        never-reach-into-private-state pattern — a caller distinguishing
        Legacy from Listen (e.g. the ``list_mcp_subscriptions`` tool's
        per-connection ``mode`` field) reads this instead of the adapter
        type directly."""
        adapter = self._subscription_adapters.get(server)
        if adapter is None:
            return None
        return "listen" if isinstance(adapter, ListenSubscriptionAdapter) else "legacy"

    def unhonored_uris(self, server: str) -> "list[str] | None":
        """Of ``subscribed_uris(server)``, the ones the server did NOT confirm
        on the most recent (re)connect — or ``None`` if honored-ness cannot be
        determined at all for this server right now (a Legacy connection,
        which has no such concept, or no successful open yet).

        #4686, per the issue's owner-approved design: the row population is
        the REQUESTED set (``subscribed_uris``), never the honored set —
        collapsing to honored-only would make a declined URI disappear from
        the screen instead of surfacing it, reproducing the exact
        "subscribed but can't tell if it's still alive" blind spot this
        issue exists to close. This method answers the complementary
        question — which of the requested URIs are *not* currently honored —
        so a caller marks the individual URI rows (``· not honored``) rather
        than collapsing the two axes into one marker (#3378 "two axes, two
        markers"). ``None`` is a THIRD, distinct state from "empty list":
        empty means every requested URI was confirmed; ``None`` means the
        service cannot say either way (render as a server-level
        ``· unconfirmed``, not a per-URI mark, since there is nothing to
        distinguish URI-by-URI)."""
        honored = self._last_honored.get(server)
        if honored is None:
            return None
        return sorted(set(self._subscriptions.get(server, ())) - honored)

    def subscription_summary(self) -> "list[dict]":
        """#4686: per-CONNECTION subscription state — one entry per held
        server with at least one subscribed URI, composed from
        :meth:`held_servers` / :meth:`subscribed_uris` /
        :meth:`subscription_mode` / :meth:`unhonored_uris` above. The single
        producer both ``Session.mcp_subscription_state`` (the TUI pane's
        read model) and ``RouterHostAdapter.mcp_list_subscriptions`` (the
        LLM-facing tool) call, so the composition logic exists in exactly
        ONE place rather than being re-derived at each of those two call
        sites and risking drift between what the operator sees and what the
        LLM reads (the issue's own explicit "don't split ①②" requirement,
        extended to the code that answers both).

        Shape: ``[{"server": name, "mode": "legacy" | "listen" | None,
        "uris": [...], "unhonored": [...] | None}, ...]``. See
        ``subscribed_uris``/``unhonored_uris``/``subscription_mode`` for the
        field-level semantics this only assembles."""
        out: "list[dict]" = []
        for server in self.held_servers():
            uris = self.subscribed_uris(server)
            if not uris:
                continue
            out.append({
                "server": server,
                "mode": self.subscription_mode(server),
                "uris": uris,
                "unhonored": self.unhonored_uris(server),
            })
        return out

    def _resolve_elicitation_bus(self) -> "RequestBus | None":
        """#2597 slice ③ D4/D6: called by an elicitation handler at request
        time (never cached) — mirrors #2095's ``consent_gate`` re-check.
        Returns None (= headless, auto-decline) when either no
        ``elicitation_bus`` was wired for this service (the default — no
        session behaviour change for any caller that doesn't opt in) or the
        wired ``elicitation_gate`` reports no live listener attached right
        now (a TUI can mount/unmount between one elicitation and the next).
        """
        if self._elicitation_bus is None:
            return None
        if self._elicitation_gate is not None and not self._elicitation_gate():
            return None
        return self._elicitation_bus

    # ── #2608 H1 / Hook-Event Phase 2 §6.1 / #2875 F1: MCP Ingress Adapter delegate ──

    def _mcp_to_hook_event(self, server: str, uri: "str | None", resync: bool) -> None:
        """The production ``ReynMCPMessageHandler.on_external_event`` callback for a
        given ``server`` (bound via ``functools.partial`` in :meth:`_ensure_open`).
        SYNCHRONOUS, non-blocking, never raises — called from the MCP receive-loop
        task (see module docstring's H1 section for the full bridge design).

        Hook-Event Redesign #2875 F1 (proposal 0059 §6 completion): converts the RAW
        signal (``uri``, ``resync`` — ``server``/``agent_name`` close over this
        service/handler) into a :class:`~reyn.hooks.event.HookEvent` via
        ``self._mcp_ingress_adapter.to_event`` — the ONE place ``build_hook_payload``
        now runs for MCP. Before #2875, ``ReynMCPMessageHandler.emit_resource_updated``
        called ``build_hook_payload`` directly and handed the finished payload to
        ``enqueue_external_event`` (below), so ``McpIngressAdapter.to_event`` — added
        in Phase 2 (#2872) — was never actually reached for MCP in production, despite
        ``FsIngressAdapter.to_event`` being reached for ``file_changed``: the §6 unify
        was 3/4 wired, not 4/4. Delivery (the bounded queue+drain-task bridge) is
        unchanged — same ``self._mcp_ingress_adapter.deliver`` this class always used."""
        event = self._mcp_ingress_adapter.to_event(
            uri, server=server, agent_name=self._agent_name, resync=resync,
        )
        self._mcp_ingress_adapter.deliver(event)

    def enqueue_external_event(self, point: str, template_vars: dict) -> None:
        """Low-level entry point onto ``self._mcp_ingress_adapter``'s bounded
        queue+drain-task delivery mechanism — takes an ALREADY-BUILT
        ``(point, template_vars)`` pair and wraps it into a
        :class:`~reyn.hooks.event.HookEvent` for delivery. Kept as a public
        entry point for exercising the bridge's queue/overflow/drain behaviour
        directly (see ``tests/hooks/test_2608_h1_mcp_resource_updated_hook.py``); the
        production MCP call path itself goes through :meth:`_mcp_to_hook_event`
        (which builds the event via ``McpIngressAdapter.to_event``, #2875 F1) —
        this method is NOT what ``ReynMCPMessageHandler`` calls any more.

        No-op when ``hook_trigger`` is None (no hook wired for this session) —
        the adapter itself no-ops in that case."""
        from reyn.hooks.event import HookEvent
        from reyn.hooks.schema_registry import canonical_kind
        self._mcp_ingress_adapter.deliver(
            HookEvent(kind=canonical_kind(point), payload=template_vars),
        )

    def _track_subscription(self, server: str, uri: str) -> None:
        self._subscriptions.setdefault(server, set()).add(uri)

    def _untrack_subscription(self, server: str, uri: str) -> None:
        self._subscriptions.get(server, set()).discard(uri)

    async def aclose(self) -> None:
        """Close every held connection. Idempotent — safe to call repeatedly (e.g. a
        session teardown seam that may run more than once)."""
        # #2608 H1 / Phase 2 §6.1: cancel the hook-event drain task FIRST
        # (finally-guaranteed, not except-Exception — CancelledError is a
        # BaseException) so a client-teardown fault below can never leave the
        # drain task orphaned across session teardown. Delegated to the
        # McpIngressAdapter (byte-identical: same cancel-then-await-swallow
        # shape, now shared with FsIngressAdapter's aclose).
        await self._mcp_ingress_adapter.aclose()

        # #3698 PR-2: cancel every proactive-reconnect-from-lost-subscription
        # task BEFORE tearing down the clients/adapters below — a task that
        # ran to completion after teardown would try to reconnect a server
        # this service just closed, resurrecting a held connection aclose()
        # was supposed to end. Same finally-guaranteed shape as the H1 drain
        # task above (CancelledError is a BaseException, not caught by a
        # bare except-Exception).
        background_tasks = list(self._background_tasks)
        self._background_tasks.clear()
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                # #4988: `await task` raises CancelledError either as that
                # task's own outcome (the `.cancel()` loop just above —
                # what this except exists to absorb) or as an independent,
                # external cancellation of THIS coroutine's own task
                # landing at the same await. `pass`-ing unconditionally
                # used to treat both the same, letting `aclose()` continue
                # (and return normally) even when its own caller was being
                # cancelled. Same discriminator as session.py's #3377
                # precedent (`_driver.cancelling() > 0`). Every task in
                # `background_tasks` already had `.cancel()` called on it
                # in the loop above, so re-raising here (stopping short of
                # awaiting the rest) does not leave any of them un-asked-
                # to-cancel — only un-joined, the same trade-off structured
                # concurrency already accepts when a caller's own task is
                # cancelled mid-cleanup elsewhere in this codebase.
                _current = asyncio.current_task()
                if _current is not None and _current.cancelling() > 0:
                    raise

        # #3698 PR-2: close every subscription adapter's own background
        # delivery machinery (a ListenSubscriptionAdapter's consumer task)
        # before tearing down the clients themselves — best-effort, mirrors
        # the client-teardown fault-tolerance immediately below.
        adapters = list(self._subscription_adapters.items())
        self._subscription_adapters.clear()
        for name, adapter in adapters:
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001 — best-effort; the client teardown below still runs regardless
                logger.warning(
                    "MCPConnectionService: teardown of subscription adapter for %r "
                    "contained an error", name, exc_info=True,
                )

        clients = list(self._clients.items())
        self._clients.clear()
        self._handles.clear()
        for name, client in clients:
            try:
                await client.__aexit__(None, None, None)
            except BaseException as exc:  # noqa: BLE001 — fault isolation mirrors MCPClientPool.__aexit__
                if is_real_control_flow(exc):
                    raise
                logger.warning(
                    "MCPConnectionService: teardown of %r contained: %r",
                    name, describe_fault(exc),
                )

    async def __aenter__(self) -> "MCPConnectionService":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


class _HeldConnection:
    """Duck-typed drop-in for :class:`MCPClient`, returned by
    :meth:`MCPConnectionService.get`. Exposes exactly the surface
    :class:`~reyn.mcp.gateway.MCPGateway` calls (``call_tool`` / ``list_tools`` /
    ``list_resources`` / ``list_resource_templates`` / ``read_resource`` /
    ``subscribe_resource`` / ``unsubscribe_resource`` / ``list_prompts`` /
    ``get_prompt`` / ``is_initialized``) so it's
    usable anywhere a bare ``MCPClient`` is expected.

    Looks up the currently-live held ``MCPClient`` by server name on every call
    instead of binding to one instance at construction time, so this handle's
    identity stays stable across the WHOLE connection lifetime — including through
    a reconnect (see :meth:`_with_reconnect`). A caller that stashed an earlier
    ``get()`` result keeps working after a reconnect without calling ``get()``
    again.
    """

    def __init__(
        self,
        service: MCPConnectionService,
        server: str,
        config: dict,
        agent_id: str | None,
    ) -> None:
        self._service = service
        self._server = server
        self._config = config
        self._agent_id = agent_id

    def is_initialized(self) -> bool:
        client = self._service._clients.get(self._server)
        return client is not None and client.is_initialized()

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        progress_callback: Any = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        # Reconnect-then-propagate (heal_only): a tool call is potentially
        # side-effectful, so on a transport MCPError we HEAL the connection (for the
        # next call) but RE-RAISE — never re-run the call — to preserve at-most-once
        # (a mid-execution drop must not double-execute the tool). See module docstring.
        return await self._heal(
            lambda c: c.call_tool(
                name, args, progress_callback=progress_callback, timeout_seconds=timeout_seconds,
            ),
            heal_only=True,
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        # Retry-once: tools/list is an idempotent read, safe to re-run on the fresh
        # connection, so it heals transparently (no user-visible failure).
        return await self._heal(lambda c: c.list_tools(), heal_only=False)

    # #2597 slice ②a: resources consumption. All three are idempotent READS (no
    # server-side side effect), so — like list_tools, unlike side-effectful call_tool
    # — they heal with heal_only=False (retry-once on the fresh connection). A resource
    # read/list re-run after a mid-call transport drop is safe (at-most-once is not a
    # concern for a pure read), so the healed connection serves the retry transparently.
    async def list_resources(self) -> list[dict[str, Any]]:
        return await self._heal(lambda c: c.list_resources(), heal_only=False)

    async def list_resource_templates(self) -> list[dict[str, Any]]:
        return await self._heal(lambda c: c.list_resource_templates(), heal_only=False)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self._heal(lambda c: c.read_resource(uri), heal_only=False)

    # #2597 slice ②c: prompts consumption. Both are idempotent READS (no
    # server-side side effect), so — like list_resources/read_resource above —
    # they heal with heal_only=False (retry-once on the fresh connection).
    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._heal(lambda c: c.list_prompts(), heal_only=False)

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._heal(lambda c: c.get_prompt(name, arguments), heal_only=False)

    # #2597 slice ②b: resource subscriptions. Unlike call_tool, subscribe/unsubscribe
    # are connection-MANAGEMENT operations, not data reads — but they still go through
    # _heal (heal_only=False) rather than a bespoke path: if the connection is dead,
    # heal reconnects it (which — via MCPConnectionService._ensure_open — re-issues
    # subscribe for every ALREADY-tracked URI, but NOT this one, since it is only
    # tracked below AFTER the call succeeds) and then _heal's heal_only=False retries
    # THIS call once on the fresh connection. That sequencing is what avoids a double
    # subscribe: the reconnect's re-subscribe loop and this method's own retry never
    # target the same URI in the same pass.
    #
    # #3698 PR-2: routed through whichever adapter is CURRENTLY active for
    # this server (rebuilt fresh by every _ensure_open, including inside
    # _heal's own reconnect path above). The legacy adapter has no
    # incremental primitive of its own — MCPClient.subscribe_resource(uri)
    # unchanged, exactly as before PR-2. The listen adapter's `Client.
    # listen()` takes its FULL filter set at open time (no incremental
    # add/remove exists on the installed SDK — measured live, see
    # subscription_port.py) — so adding/removing ONE URI means closing and
    # re-opening the stream with the updated FULL tracked set, which
    # ListenSubscriptionAdapter.open() already does internally (see its own
    # docstring).
    async def subscribe_resource(self, uri: str) -> None:
        async def op(c: MCPClient) -> None:
            adapter = self._service._subscription_adapters.get(self._server)
            if isinstance(adapter, ListenSubscriptionAdapter):
                all_uris = set(self._service._subscriptions.get(self._server, ())) | {uri}
                # #4686: this IS the honored-computing open() call for an
                # incremental add — _ensure_open's own honored-storage line
                # only runs at (re)connect time, never here, so without this
                # ``unhonored_uris`` would read a STALE (pre-this-URI)
                # honored set for every subscribe added to an
                # already-open Listen connection.
                self._service._last_honored[self._server] = await adapter.open(all_uris)
            else:
                await c.subscribe_resource(uri)

        await self._heal(op, heal_only=False)
        self._service._track_subscription(self._server, uri)

    async def unsubscribe_resource(self, uri: str) -> None:
        async def op(c: MCPClient) -> None:
            adapter = self._service._subscription_adapters.get(self._server)
            if isinstance(adapter, ListenSubscriptionAdapter):
                remaining = set(self._service._subscriptions.get(self._server, ())) - {uri}
                # #4686: mirrors subscribe_resource's own honored-storage —
                # see that method's comment.
                self._service._last_honored[self._server] = await adapter.open(remaining)
            else:
                await c.unsubscribe_resource(uri)

        await self._heal(op, heal_only=False)
        self._service._untrack_subscription(self._server, uri)

    async def _heal(
        self, op: "Callable[[MCPClient], Awaitable[Any]]", *, heal_only: bool,
    ) -> Any:
        """Run ``op`` against the currently-held client. On an :class:`MCPTransportError`
        — genuine transport-death, the only signal that actually means the held
        connection is dead (see module docstring + ``client.py``'s ``_is_transport_death``
        predicate) — discard + reopen the connection.

        #2597 F1: this deliberately catches ONLY ``MCPTransportError``, not the base
        ``MCPError``. Post-S1, every ``MCPClient`` method wraps ALL exceptions into some
        ``MCPError`` subclass, so a bare ``except MCPError:`` here used to over-catch two
        cases that are NOT a dead connection: a capability-gate refusal
        (``MCPCapabilityError`` — the server is alive, reyn just declined to send the
        request) and an application-level protocol error (unknown tool/resource, invalid
        params — the server responded, just with an error). Both of those propagate
        WITHOUT touching the connection now; only ``MCPTransportError`` triggers
        discard+reopen.

        ``heal_only=True`` (side-effectful ``call_tool``): re-raise the ORIGINAL error
        after healing — do NOT re-run ``op`` (preserves at-most-once; the healed
        connection serves the NEXT call). ``heal_only=False`` (idempotent ``list_tools``):
        retry ``op`` ONCE on the fresh connection; a second failure propagates unchanged
        (no silent retry loop).

        #3698 review: ``_ensure_open``/``_reconnect`` are now called under
        ``self._service._lock_for(self._server)`` — the SAME per-server lock
        :meth:`MCPConnectionService.get` already uses. Closes a real,
        live-verified double-reconnect race: a single transport death used
        to fire BOTH this method's own reactive reconnect (an in-flight call
        failing) AND, independently, ``_on_subscription_lost``'s proactive
        one (the listen stream noticing loss) — see that method's own
        docstring for the full finding. ``op(client)`` itself runs OUTSIDE
        the lock (only connection-state mutation is serialized — unrelated
        concurrent calls on an already-healthy connection are not
        artificially serialized against each other)."""
        async with self._service._lock_for(self._server):
            client = await self._service._ensure_open(
                self._server, self._config, agent_id=self._agent_id,
            )
        try:
            return await op(client)
        except MCPTransportError:
            async with self._service._lock_for(self._server):
                # #3698 review: staleness check mirrors
                # ``_reconnect_from_lost_subscription``'s — the SAME
                # transport death that just failed ``op(client)`` above may
                # have ALSO already been reconnected by the proactive
                # ``_on_subscription_lost`` path while this method was
                # waiting for the lock. If so, reuse that already-fresh
                # client rather than discarding it for a third, redundant
                # reconnect.
                current = self._service._clients.get(self._server)
                if current is not None and current is not client:
                    fresh = current
                else:
                    fresh = await self._service._reconnect(
                        self._server, self._config, agent_id=self._agent_id,
                    )
            if heal_only:
                raise  # at-most-once: connection healed for the next call, but this call is NOT retried
            return await op(fresh)
