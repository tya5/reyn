"""Subscription port (#3698 PR-2) — a version-independent abstraction over
resource-subscription and list_changed delivery.

## Why a port

PR-1 pinned ``Client(mode="legacy")`` deliberately: the official SDK's
``mode="auto"`` negotiates UP to a modern (2026-07-28-era) protocol version
whenever the peer nominally advertises it, and under that revision
``resources/subscribe`` and the legacy push-based ``tools/prompts
list_changed`` notifications are BOTH replaced by a single streaming
primitive, ``Client.listen()`` (SEP-2575) — a server/client pair that only
implements the legacy mechanism silently stops delivering anything once
negotiation moves modern (see ``client.py``'s "mode='legacy', not the SDK's
own 'auto' default" module-docstring section for the two live-verified
symptoms this caused). This module is what makes ``mode="auto"`` safe again:
a single ``SubscriptionPort`` object per held connection that picks the
adapter matching the ACTUAL negotiated version, so the caller
(``MCPConnectionService``) never branches on protocol version itself.

**Under a modern-era (2026-07-28+) negotiation, the legacy ``resources/
subscribe``/``resources/unsubscribe`` RPCs do not exist on the wire at
all** — not "deprecated but still answered", genuinely removed; the SDK's
own ``Client.subscribe_resource``/``unsubscribe_resource`` raise a wire-level
``MCPError`` ("Method not found") when called against a modern-negotiated
session, REGARDLESS of whether the server also implements ``Client.
listen()``. This was undocumented anywhere in reyn until a real PR-2 fork
(#3698 review) surfaced it via a live 404: adding the modern
``subscriptions/listen`` handler to a test double made its capability
advertisement flip modern-nego-eligible, and the ONE caller that still
invoked the legacy RPC directly (bypassing this port —
``MCPClient.subscribe_resource``/``unsubscribe_resource`` themselves, before
this fix) broke. The fix: era selection is monopolized by THIS module —
every ``subscribe_resource``/``unsubscribe_resource`` call, whether through
a held :class:`~reyn.mcp.connection_service.MCPConnectionService` connection
or a bare ephemeral :class:`~reyn.mcp.client.MCPClient`, routes through
:func:`select_subscription_adapter` (see that function's own docstring and
``client.py``'s ``subscribe_resource``/``unsubscribe_resource``) — no caller
ever needs to know or branch on the negotiated version itself, and no
caller can accidentally reach the legacy RPC once negotiation has gone
modern.

## The two adapters

- ``LegacySubscriptionAdapter`` — pre-2026-07-28. ``open()``/``add()`` call
  ``MCPClient.subscribe_resource(uri)`` per URI (unchanged from pre-PR-2
  behavior); ``honored`` is always ``None`` — a legacy server's
  ``EmptyResult`` ack carries no "which URIs did you actually accept"
  signal, so the port is honest about not knowing rather than lying with a
  bool. list_changed notifications arrive via the existing
  ``ReynMCPMessageHandler`` message-handler push — this adapter does
  nothing extra for those (the handler is already wired into ``Client``
  regardless of adapter).
- ``ListenSubscriptionAdapter`` — 2026-07-28-era. ``open()``/``add()``
  (re)open ONE ``Client.listen(resource_subscriptions=..., tools_list_
  changed=True, prompts_list_changed=True)`` stream covering all three
  families in a single call (confirmed live: the SDK's own ``listen()``
  accepts all three filter kwargs together — see the design record below).
  ``honored`` is ``sub.honored.resource_subscriptions`` turned into a
  ``set[str]`` (``None`` when the server declined resource_subscriptions
  entirely). A background task consumes the stream and re-dispatches each
  event to the SAME ``ReynMCPMessageHandler`` methods
  (``on_tool_list_changed``/``on_prompt_list_changed``/
  ``emit_resource_updated``) the legacy message_handler push already used
  — one producer swap, zero new consumer-side code, so every existing
  ``mcp_tool_list_changed``/``mcp_prompt_list_changed``/
  ``mcp_resource_updated`` audit-event/hook-trigger consumer fires
  identically regardless of which era delivered the underlying signal. A
  ``SubscriptionLost`` (the stream ending abnormally — transport death or a
  server-side drop) is treated as connection death: it invokes the
  ``on_lost`` callback the owner supplied, which ``MCPConnectionService``
  wires to trigger the SAME reconnect path an ``MCPTransportError`` on a
  live call already does (see that module's ``_on_subscription_lost``).

``honored`` is NEVER collapsed to a bool anywhere in this module (architect's
explicit design constraint, #3698 comments): a legacy "I don't know" and a
modern "the server declined" are both real, DIFFERENT states a caller might
need to distinguish, and a bool would silently turn the legacy "unknown"
into "succeeded" — exactly the "declared implies used" misreading this
repo's own testing discipline names as a recurring failure shape.

## Design record: three SDK-level findings from getting here (kept for PR-3)

1. ``asyncio.wait_for`` wrapping ``Client.__aenter__()`` (PR-1) raises a
   cancel-scope-nesting violation on success and hangs on a genuine
   timeout — ``asyncio.wait_for`` around ``Client.listen()`` entry/
   iteration, by contrast, was live-verified to behave correctly (no
   analogous corruption) — this is NOT a blanket "never wrap anyio calls in
   wait_for" rule; each shape needed its own live check.
2a. ``asyncio.wait_for`` wrapping a ``Client.listen()`` context manager's
   ``__aexit__()`` (PR-2, #3698 review) against a KNOWN-DEAD transport
   (peer killed via ``os._exit()`` — no graceful close is possible) was
   live-verified to hang INDEFINITELY (>90s, no exception ever raised) —
   NOT merely slow. This is a second, independent instance of finding 1's
   exact shape: cancelling an anyio-scoped SDK call from outside its owning
   task, on timeout, hangs instead of raising cleanly. Combined with
   finding 1: **this SDK's enter/exit cannot be safely bounded from the
   outside by time, in either direction** — the fix each time was removing
   the call from the code path that reaches a known-dead peer entirely
   (:meth:`ListenSubscriptionAdapter.close`'s ``graceful=False`` — see its
   own docstring), never a shorter/differently-wrapped deadline. A future
   caller reaching for ``wait_for``/``anyio.fail_after`` around ANY
   ``Client``/``Subscription`` enter-or-exit call should treat that as a
   near-certain repeat of this same hazard, not a fresh case to
   individually re-verify from zero.
2b. ``Client.listen()``'s honored-ack succeeding is NOT proof an event will
   ever arrive: the SDK provides ``InMemorySubscriptionBus`` as a fan-out
   SEAM, but publishing to it is the SERVER AUTHOR's own responsibility —
   ``MCPServer``'s own source never calls ``bus.publish(...)`` automatically
   on a tool author's behalf (grepped directly: zero call sites). reyn's own
   two MCP test doubles (``tests/_support/mcp_fastmcp_echo_server.py``,
   ``mcp_subscribable_resources_server.py``) had to be updated to publish
   explicitly — see their own module docstrings for the live-verified
   before/after (a minimal bare-SDK server built specifically to isolate
   this, calling ``bus.publish()`` directly, delivered the event instantly;
   the ORIGINAL test doubles, using only the legacy ``send_notification``
   API, hung a listening client forever with zero error). This was traced
   to reyn's OWN test infrastructure, not an SDK defect (a bare-SDK
   client+server reproduction, publishing correctly, worked with zero
   hang) — no upstream report was needed.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Protocol

from mcp.client.subscriptions import SubscriptionLost
from mcp.shared.subscriptions import (
    PromptsListChanged,
    ResourcesListChanged,
    ResourceUpdated,
    ToolsListChanged,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

if TYPE_CHECKING:
    from reyn.mcp.client import MCPClient
    from reyn.mcp.message_handler import ReynMCPMessageHandler

logger = logging.getLogger(__name__)

#: The sync, non-blocking callback a :class:`ListenSubscriptionAdapter` fires
#: when its stream ends via :class:`~mcp.client.subscriptions.SubscriptionLost`
#: — mirrors #2608 H1's sync-callback-into-async-work bridge shape
#: (``MCPConnectionService.enqueue_external_event``'s own pattern): the
#: adapter's background task cannot itself own reconnect orchestration (that
#: is ``MCPConnectionService``'s job, and the adapter must not depend on it
#: to avoid a layering cycle), so it hands back a plain notification.
OnSubscriptionLost = Callable[[], None]


def is_modern_protocol_version(version: "str | None") -> bool:
    """True iff ``version`` (an :attr:`MCPClient.negotiated_version` read)
    is a 2026-07-28-era protocol version — the ONE predicate that decides
    which adapter :func:`select_subscription_adapter` builds. Duck-typed
    against the SDK's own ``MODERN_PROTOCOL_VERSIONS`` tuple (not a
    hardcoded string), mirroring ``_mcp_client_boundary.py``'s "read the
    SDK's own vocabulary, never re-derive it" convention. ``None`` (no
    connection negotiated yet) is never modern."""
    return version in MODERN_PROTOCOL_VERSIONS


class SubscriptionAdapter(Protocol):
    """Version-independent subscription/list-changed delivery port. One
    instance per held connection, rebuilt on every (re)connect by
    :func:`select_subscription_adapter` — see module docstring."""

    async def open(self, uris: "set[str]") -> "set[str] | None":
        """(Re)establish delivery for the FULL desired ``uris`` set — called
        at initial connect and on every reconnect (never incrementally; the
        caller always passes the complete tracked set). Returns the honored
        subset, or ``None`` if this adapter cannot report honored-ness."""
        ...

    async def close(self, *, graceful: bool = True) -> None:
        """Tear down any background delivery machinery (a no-op for the
        legacy adapter, which owns none).

        ``graceful`` (#3698 review ruling): ``True`` (default) attempts a
        polite close with the peer — the right choice for a LIVE connection
        (e.g. a URI-change reopen). Pass ``graceful=False`` ONLY when the
        caller already knows the transport is dead (there is no peer left
        to agree a close with) — see :class:`ListenSubscriptionAdapter`'s
        own docstring for why this exists at all: a graceful close attempted
        against a known-dead transport was observed to hang indefinitely,
        and bounding it with ``asyncio.wait_for`` reproduces the SAME
        cancel-scope hazard PR-1 already documented for ``Client.
        __aenter__`` — the fix is "don't ask a dead peer for a graceful
        goodbye" (skip the round-trip entirely), not "ask with a shorter
        deadline"."""
        ...


class LegacySubscriptionAdapter:
    """Pre-2026-07-28: per-URI ``subscribe_resource`` calls, unchanged from
    pre-PR-2 behavior. ``honored`` is always ``None`` (see module docstring).
    list_changed notifications arrive via the existing message_handler push
    — this adapter is a pure pass-through for that; it does nothing beyond
    the subscribe calls themselves.

    Calls ``client._raw_subscribe_resource_rpc`` (a private, same-package
    helper on :class:`~reyn.mcp.client.MCPClient` — NOT the public
    ``subscribe_resource``) deliberately: the public method is itself what
    SELECTS this adapter (see ``client.py``'s ``subscribe_resource``), so
    calling back into it here would recurse. The private helper is the ONE
    place the legacy RPC is actually issued, shared by both entry points —
    this adapter and ``MCPClient``'s own public method never carry two
    copies of that call (see module docstring's "Why a port" for why this
    split exists at all: the legacy RPC does not exist on the wire once
    negotiation is modern, so calling it is only ever safe from inside an
    adapter this module itself selected for a legacy-negotiated
    connection)."""

    def __init__(self, client: "MCPClient") -> None:
        self._client = client

    async def open(self, uris: "set[str]") -> "set[str] | None":
        for uri in uris:
            await self._client._raw_subscribe_resource_rpc(uri)  # noqa: SLF001 — see docstring
        return None

    async def close(self, *, graceful: bool = True) -> None:
        return None


class ListenSubscriptionAdapter:
    """2026-07-28-era: one ``Client.listen()`` stream covering all three
    families (tools_list_changed / prompts_list_changed /
    resource_subscriptions), consumed by a background task that re-dispatches
    each event to ``handler``'s existing methods. See module docstring for
    the full design and the two SDK-level findings that shaped it.

    ``on_lost`` (see :data:`OnSubscriptionLost`) fires exactly once per
    ``open()`` call if the stream ends via ``SubscriptionLost`` — never on a
    graceful ``close()`` (the caller-initiated teardown path, e.g. a URI
    being added mid-session forces a close+reopen; that is NOT a loss).

    ``handler`` may be ``None`` — a bare :class:`~reyn.mcp.client.MCPClient`
    with no ``ReynMCPMessageHandler`` installed (the ephemeral one-shot
    ``MCPClientPool`` path, or any standalone use) still needs to open a
    real ``listen()`` stream once negotiation is modern (the legacy RPC
    genuinely does not exist there — see module docstring), even though
    nothing local consumes the delivered events. ``_dispatch`` is a no-op
    in that case: the subscription is registered server-side (the point of
    ``open()``), but events are dropped rather than routed anywhere — the
    same observable outcome a handler-less legacy caller already had
    (nothing locally consumed pushes either)."""

    def __init__(
        self,
        client: "MCPClient",
        handler: "ReynMCPMessageHandler | None",
        *,
        on_lost: "OnSubscriptionLost | None" = None,
    ) -> None:
        self._client = client
        self._handler = handler
        self._on_lost = on_lost
        self._cm: "Any | None" = None
        self._task: "asyncio.Task[None] | None" = None

    async def open(self, uris: "set[str]") -> "set[str] | None":
        # Re-opening (a new URI added, or a reconnect) always closes any
        # prior stream first — the SDK's listen() takes the full filter set
        # at open time, no incremental add/remove primitive exists (measured
        # live against the installed SDK), so "add one URI" is structurally
        # "close, then reopen with the updated full set" for this adapter.
        await self.close()
        raw_client = self._client._client  # the entered mcp.Client — see client.py's own module docstring for why this is the stable internal handle
        self._cm = raw_client.listen(
            resource_subscriptions=list(uris),
            tools_list_changed=True,
            prompts_list_changed=True,
        )
        sub = await self._cm.__aenter__()
        honored_uris = sub.honored.resource_subscriptions
        if self._handler is not None:
            # #3698 PR-2 (review ruling): tell the SAME handler which
            # families THIS open() call actually got the server to honor
            # over listen, so its legacy __call__ dispatch (still bound to
            # the raw Client regardless of era) suppresses only those
            # families — see ReynMCPMessageHandler.set_listen_honored's
            # docstring for why this exists (a dual-firing server would
            # otherwise double-deliver). `bool(...)` on the two list_changed
            # fields folds both `False` and `None` ("unknown") to "not
            # honored" — the ruling's "honored が None なら何も抑止しない".
            self._handler.set_listen_honored(
                tools=bool(sub.honored.tools_list_changed),
                prompts=bool(sub.honored.prompts_list_changed),
                resources=honored_uris is not None,
            )
        self._task = asyncio.create_task(self._consume(sub))
        return set(honored_uris) if honored_uris is not None else None

    async def _consume(self, sub: "Any") -> None:
        try:
            async for event in sub:
                await self._dispatch(event)
        except SubscriptionLost:
            server_name = (
                self._handler._server_name  # noqa: SLF001 — same-package internal read, mirrors connection_service.py's existing pattern
                if self._handler is not None
                else self._client.server_name
            )
            logger.warning(
                "ListenSubscriptionAdapter: subscription lost for %r", server_name,
            )
            if self._on_lost is not None:
                try:
                    self._on_lost()
                except Exception:  # noqa: BLE001 — a faulting callback must not crash the consumer task's own teardown
                    logger.warning(
                        "ListenSubscriptionAdapter: on_lost callback failed", exc_info=True,
                    )
        except asyncio.CancelledError:
            # A deliberate close() (URI change, teardown) cancels this task —
            # NOT a loss, no on_lost callback, propagate the cancellation.
            raise

    async def _dispatch(self, event: "Any") -> None:
        # Re-dispatch to the SAME handler methods the legacy message_handler
        # push already calls — see module docstring. None of these methods
        # actually read their `message` argument's content (verified against
        # message_handler.py: on_tool_list_changed/on_prompt_list_changed
        # ignore it entirely), so passing None here is not a shape mismatch.
        if self._handler is None:
            # No local consumer (see __init__'s docstring) — the
            # subscription is real server-side, the event is just dropped.
            return
        if isinstance(event, ToolsListChanged):
            await self._handler.on_tool_list_changed(None)
        elif isinstance(event, PromptsListChanged):
            await self._handler.on_prompt_list_changed(None)
        elif isinstance(event, ResourceUpdated):
            self._handler.emit_resource_updated(event.uri, resync=False)
        elif isinstance(event, ResourcesListChanged):
            # Out of scope, same as the legacy path (message_handler.py's
            # own docstring: "no reyn caller subscribes to the resource LIST
            # changing, only individual resource content updates").
            pass

    async def close(self, *, graceful: bool = True) -> None:
        # #3698 review — condition ②: the drain task is ALWAYS explicitly
        # cancelled here (never just dropped by reference), regardless of
        # ``graceful`` — that's what actually stops the background consumer;
        # ``graceful`` only controls whether the STREAM's own SDK-level
        # close (below) is attempted.
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # #4988: `await self._task` raises CancelledError either
                # as that task's own outcome (this block's own `.cancel()`
                # two lines up — what this except exists to absorb) or as
                # an independent, external cancellation of THIS
                # coroutine's own task landing at the same await.
                # Catching it unconditionally (formerly folded into the
                # `SubscriptionLost` tuple below) used to treat both the
                # same, letting `close()` continue as if it had completed
                # even when its own caller was being cancelled. Same
                # discriminator as session.py's #3377 precedent
                # (`_driver.cancelling() > 0`).
                _current = asyncio.current_task()
                if _current is not None and _current.cancelling() > 0:
                    raise
            except SubscriptionLost:
                pass
            self._task = None
        if self._cm is not None:
            cm = self._cm
            self._cm = None
            if not graceful:
                # #3698 review ruling: a caller passes graceful=False ONLY
                # when it already knows the transport is dead (currently:
                # MCPConnectionService._reconnect's old-adapter cleanup) —
                # there is no peer left to round-trip a graceful
                # ``subscriptions/listen`` close with, so don't ask for one.
                # An EARLIER version of this method unconditionally called
                # ``cm.__aexit__()`` here, live-verified to hang
                # INDEFINITELY (>90s, no exception) against a known-dead
                # transport; bounding it with ``asyncio.wait_for`` was tried
                # and REJECTED (ruling) — that reproduces the exact
                # cancel-scope hazard PR-1 already documented for
                # ``Client.__aenter__`` (cancelling an anyio-scoped SDK call
                # from outside its owning task can hang instead of raising
                # cleanly), now confirmed for ``__aexit__`` too — see this
                # module's own docstring "Design record" section: this
                # SDK's enter/exit cannot be bounded from the outside by
                # time — two independent instances of the same hazard. The
                # correct fix is not calling it at all for a known-dead
                # peer, not a shorter deadline.
                return
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — best-effort; a live transport's own close can still fault
                logger.warning("ListenSubscriptionAdapter: listen stream teardown faulted", exc_info=True)


def select_subscription_adapter(
    client: "MCPClient",
    handler: "ReynMCPMessageHandler | None",
    *,
    on_lost: "OnSubscriptionLost | None" = None,
) -> "SubscriptionAdapter":
    """Build the adapter matching ``client``'s ACTUAL negotiated version —
    the ONE decision point every #3698 PR-2 acceptance witness checks
    (adapter SELECTION, not just "both work" — see ``connection_service.py``
    for where this is called and the audit-event that records which one
    was picked), and — per the #3698 review ruling this function's own
    history now records — the ONLY place that decision is made. Every
    ``subscribe_resource``/``unsubscribe_resource`` caller (a held
    ``MCPConnectionService`` connection, or a bare ephemeral
    :class:`~reyn.mcp.client.MCPClient`) routes through this function; none
    branches on ``negotiated_version`` itself.

    Selection is purely version-based — ``handler`` never gates WHICH
    adapter is picked, only what the picked adapter does with delivered
    events. An earlier version of this function returned the legacy
    adapter whenever ``handler is None``, on the assumption that "a legacy
    per-URI ``subscribe_resource`` call still works fine standalone" — that
    assumption was FALSIFIED live (#3698 review): the legacy RPC does not
    exist on the wire once negotiation is modern, regardless of whether a
    handler is installed (see module docstring's "Why a port"), so a
    handler-less caller on a modern-negotiated connection needs the listen
    adapter exactly as much as a handler-having one does — it just gets no
    local dispatch (see :class:`ListenSubscriptionAdapter`'s own
    docstring)."""
    if is_modern_protocol_version(client.negotiated_version):
        return ListenSubscriptionAdapter(client, handler, on_lost=on_lost)
    return LegacySubscriptionAdapter(client)


__all__ = [
    "LegacySubscriptionAdapter",
    "ListenSubscriptionAdapter",
    "OnSubscriptionLost",
    "SubscriptionAdapter",
    "is_modern_protocol_version",
    "select_subscription_adapter",
]
