"""FastAPI AG-UI transport endpoint — HTTP+SSE, behind the P0 auth gate (P2/P3).

The wire surface for every UI client (D2): the local CUI, the remote thin
client, AND the openui browser. An SSE endpoint that streams the server
session's :class:`~reyn.interfaces.transport.frames.Frame` stream as AG-UI events
(via :class:`~reyn.interfaces.transport.agui.emitter.AgUiEmitter`), plus a POST
for client→server turn submit, HITL answers, cancel, seize, and heartbeat. It is
modelled on the existing A2A SSE pattern (``StreamingResponse`` /
``text/event-stream``) — A2A is the internal spine (D1); this is the SINGLE
AG-UI UI surface (the legacy per-client WebSocket chat route was retired once the
browser migrated here).

Every connection is gated by the **P0 auth context** (``app.state.auth``): the
request identity is resolved through the SAME
:meth:`~reyn.interfaces.web.auth.core.AuthContext.authenticate` seam the WS gate
uses (no new auth) — an unauthenticated connection is refused before any session
is attached. P3 adds the load-bearing safety half on top of that gate:

- **HITL answer round-trip (R1 by-id).** A ``TOOL_CALL_RESULT`` POST correlates to
  its intervention by ``toolCallId`` (= the intervention id); the server
  re-authorizes at delivery (identity + active-driver token) and resolves BY ID —
  an unknown / already-resolved id is a typed reject, never a head fallback.
- **Answering = a permission grant.** Delivery-time server-side
  ``authorize_write(identity)`` (the client is UNTRUSTED — re-authorize, never
  trust a client-asserted identity), then ``external_source=False`` for the
  authenticated human operator (unfenced, the P0 keystone).
- **Active-driver token + symmetric seize (D4).** One connection holds interactive
  authority; any authorized surface may seize; a deposed holder's late answer is
  rejected at delivery (the active-driver check).
- **Unified fail-close (D5b).** A pending intervention whose last answerable
  operator surface is lost — in-proc detach OR heartbeat timeout — is typed-DENY'd
  after a grace window T (not parked); a reconnect within T keeps it pending.
- **Attribution.** ``user_answered_intervention`` carries ``auth_user_id`` + the
  connection id; ``client_attached`` / ``client_seized`` / ``client_detached`` land
  on the P6 audit trail.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
import weakref
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from reyn.core.events.events import Event
from reyn.interfaces.inline.textual_chat.restore import (
    page_restored_history,
    project_restored_frames,
)
from reyn.interfaces.repl.status import _snapshot_for_session
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import encode_messages_snapshot
from reyn.interfaces.transport.agui.surface import (
    SurfaceManager,
    monotonic,
    surface_registry,
)
from reyn.interfaces.transport.drain import suspend_between_frames
from reyn.interfaces.transport.frames import (
    HYDRATE_PAGE_FRAMES,
    DisplayFrame,
    EventFrame,
    Frame,
    forwarded_frame_kinds,
)
from reyn.interfaces.web.auth import AuthContext, ConnectionIdentity
from reyn.interfaces.web.deps import get_registry
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.outbox_hub import DEFAULT_SURFACE_MAXSIZE
from reyn.runtime.registry import _DEFAULT_SID
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
from reyn.runtime.session_buses import NO_SURFACE_REFUSAL_REASON

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agui"])

# The operator surface's intervention-listener channel. The SAME id the session
# stamps chat ivs with (``_build_intervention_bus_for_run`` → ``origin_channel_id
# = "tui"``) and the in-process transport binds — so the AG-UI operator surface is
# the same channel class as the local operator, and fail-close scoping
# (per-intervention, R2) skips A2A-origin-pin ivs whose own listener is still live.
AGUI_OPERATOR_CHANNEL = DEFAULT_CHAT_CHANNEL_ID

# Per-agent fail-close driver tasks (module-global; single-writer server).
_DRIVERS: "dict[str, asyncio.Task]" = {}


class _ConnectionRetargetHub:
    """#5116: cross-agent ``/attach`` notification, keyed by ``connection_id``.

    ``registry.add_attach_listener`` (agent-keyed) answers "who is watching
    agent X's session focus" — correct for a session-switch WITHIN the
    agent a connection already watches, structurally unable to answer "tell
    connection C it should now watch a DIFFERENT agent" (nobody is ever
    listening under the target's own key, since no connection is open to
    that URL). This hub answers the second question, the ``attach_request``
    POST handler (a DIFFERENT HTTP request than the SSE stream it targets,
    correlated only by ``connection_id``) is the one caller with both the
    connection id AND the resolved attach target, so it is the one caller
    of :meth:`notify`. Module-global (single-writer server, mirrors
    ``_DRIVERS`` above) — one hub for the whole process, not one per
    connection (a connection does not know its own id exists as a
    notification key until it subscribes)."""

    def __init__(self) -> None:
        self._listeners: "dict[str, list[Callable[[str, str], None]]]" = {}
        # #5129: THE per-connection "what agent is this connection on right
        # now" fact, addressable by a request that isn't the SSE stream
        # itself (a POST, correlated only by connection_id — the same
        # constraint that motivated this hub in the first place, #5116's own
        # docstring). Seeded in :meth:`subscribe` (the connection's own URL
        # agent, so a connection that never attaches still resolves) and
        # kept current in :meth:`notify` (the same call that already tells
        # this hub's listeners a retarget happened) — one write path each,
        # not a second copy of the fact ``_SessionFrameSource.current_agent_
        # name`` already owns for the SSE-stream reader itself.
        self._current_agent: "dict[str, str]" = {}

    def subscribe(
        self, connection_id: str, callback: "Callable[[str, str], None]", agent_name: str,
    ) -> None:
        """*agent_name* seeds :meth:`current_agent` to THIS connection's own
        URL agent — required so a connection that never sends
        ``attach_request`` still resolves correctly (an empty/missing seed
        would read as "unknown", and #5129's ``agui_submit`` fallback would
        then fall back to ITS OWN URL param anyway — but future callers of
        :meth:`current_agent` should not have to know that; the seed makes
        the value simply always correct from the moment of subscribe). A
        reconnect calls :meth:`subscribe` again (a fresh SSE GET), correctly
        re-seeding to that connect's own URL agent.

        No default (lead-coder co-vet, PR #5132 review): an optional
        ``agent_name=""`` let a caller subscribe WITHOUT seeding, and
        ``current_agent``'s ``None`` would then carry two meanings — "never
        seen" and "subscribed, not seeded" — the second silently falling
        back to the URL again, #5129's own symptom, with no red anywhere
        (the #4996/#5093-family shape CLAUDE.md names: one value standing in
        for two facts). This hub has exactly one caller
        (:meth:`_SessionFrameSource.listen_for_retarget`); making the
        parameter required closes the second meaning by construction rather
        than by convention."""
        self._listeners.setdefault(connection_id, []).append(callback)
        self._current_agent[connection_id] = agent_name

    def unsubscribe(self, connection_id: str, callback: "Callable[[str, str], None]") -> None:
        listeners = self._listeners.get(connection_id)
        if not listeners or callback not in listeners:
            return
        listeners.remove(callback)
        if not listeners:
            del self._listeners[connection_id]
            # #5129: same lifetime as the listener entry it was seeded
            # alongside — an unbounded dict keyed by connection_id was
            # already #5119's own finding about this hub; this fact does
            # not get a longer life than the subscription that seeded it.
            self._current_agent.pop(connection_id, None)

    def notify(self, connection_id: str, agent_name: str, sid: str) -> None:
        """Fired synchronously (mirrors ``registry._announce_session_
        attached``'s own no-await critical section) — a listener must not
        block or await; its job is to hand the payload to a side-channel a
        consumer task drains, the SAME idiom ``add_attach_listener``'s own
        docstring states.

        Only records into :attr:`_current_agent` when ``connection_id`` has
        a live subscription (architect/lead-coder co-vet, PR #5132 review):
        the ``attach_request`` POST is a DIFFERENT request than the SSE
        stream, correlated only by a CLIENT-SUPPLIED ``connection_id`` — a
        client can POST ``attach_request`` repeatedly having never opened
        (or after having closed) the matching SSE stream, and nothing here
        requires it not to (a delayed attach after the SSE dropped is a
        genuine, non-malicious case, not just a hostile one). Recording
        unconditionally would create an entry :meth:`unsubscribe` — the
        only removal path, gated on the listener list becoming empty — can
        never reach, since no listener was ever added for that id: an
        unbounded dict keyed by a value the client fully controls. Guarding
        here means an entry only ever exists for a connection this hub
        ALSO tracks a listener for, so it shares that entry's exact
        lifetime — never longer."""
        if connection_id in self._listeners:
            self._current_agent[connection_id] = agent_name
        for callback in list(self._listeners.get(connection_id, ())):
            callback(agent_name, sid)

    def has_subscribers(self, connection_id: str) -> bool:
        """#5119: the public read this class's own leak witness needs —
        mirrors :meth:`SurfaceManager.has_surfaces`'s naming. Without
        this, a test checking "did this connection's subscription leak"
        would have no way to ask except reaching into :attr:`_listeners`
        directly (CLAUDE.md's own testing policy: a test must not depend
        on private state — this method is the public surface that
        absence would otherwise BE the finding)."""
        return bool(self._listeners.get(connection_id))

    def current_agent(self, connection_id: str) -> "str | None":
        """#5129: the public read ``agui_submit`` (a DIFFERENT HTTP request
        than the SSE stream that seeded/updates this) needs to resolve "what
        agent is this connection ACTUALLY on", instead of trusting its own
        URL path param — the fix for the 8th holder #5129 names. ``None``
        for a connection this hub has never seen (no SSE stream ever
        subscribed under this id) — the caller's own fallback is the URL,
        never a guess made here."""
        return self._current_agent.get(connection_id)


_CONNECTION_RETARGET_HUB = _ConnectionRetargetHub()


def connection_retarget_has_subscribers(connection_id: str) -> bool:
    """#5119: the module-level public surface a test needs to ask "did this
    connection's ``/attach`` retarget subscription leak" — without this, the
    only way to ask is reaching into the module-private
    :data:`_CONNECTION_RETARGET_HUB` global itself (single-underscore
    module attribute; ``test_tier_audit.py``'s AST walk flags ANY attribute
    access rooted at a private name, including a public method called on
    it, so the hub instance being module-private was itself the gap — not
    just :attr:`_ConnectionRetargetHub._listeners`)."""
    return _CONNECTION_RETARGET_HUB.has_subscribers(connection_id)


def connection_current_agent(connection_id: str) -> "str | None":
    """#5129: the module-level public surface ``agui_submit`` (and any test
    of it) needs to resolve a connection's REAL current agent — mirrors
    :func:`connection_retarget_has_subscribers`'s own reason for existing at
    module level rather than requiring a private-global reach-in."""
    return _CONNECTION_RETARGET_HUB.current_agent(connection_id)


def _auth_context(request: Request) -> "AuthContext | None":
    """The process-wide AuthContext built by the server lifespan, if present."""
    return getattr(getattr(request.app, "state", None), "auth", None)


def _token_from_request(request: Request) -> "str | None":
    tok = request.query_params.get("token")
    if tok:
        return tok
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _connection_id_from_request(request: Request) -> str:
    """The client-presented connection id (Axis-B — WHICH terminal), or a fresh
    one. Read from the ``connection_id`` query param / ``X-Reyn-Connection`` header
    so the SSE GET and its sibling POSTs share one surface identity."""
    cid = request.query_params.get("connection_id") or request.headers.get(
        "x-reyn-connection"
    )
    return cid or uuid.uuid4().hex


def authenticate_request(
    request: Request, auth: AuthContext, *, connection_id: str = ""
) -> ConnectionIdentity:
    """Resolve a request's identity through the P0 ``authenticate`` seam.

    Adapts a FastAPI ``Request`` (client host + presented token) to the existing
    :meth:`AuthContext.authenticate` — no new auth logic, the WS gate's twin for
    the HTTP surface.
    """
    client = getattr(request, "client", None)
    client_host = getattr(client, "host", None) if client else None
    return auth.authenticate(
        client_host=client_host,
        presented_token=_token_from_request(request),
        connection_id=connection_id,
    )


def _authorized_predicate(auth: AuthContext):
    """Axis-A membership predicate for seize (``user_id -> bool``). v1 has a
    single operator user-id, so any non-empty authenticated user-id is in the
    authorized set; the predicate is the seam a per-user-ID authz table extends."""
    def _ok(user_id: "str | None") -> bool:
        return bool(user_id)

    return _ok


def _surface_manager(agent_name: str, auth: AuthContext) -> SurfaceManager:
    return surface_registry().for_agent(
        agent_name, authorized=_authorized_predicate(auth)
    )


# #5146: registries this process has already wired a purge-cleanup listener
# for — a WeakSet (not a bare module-global bool) so wiring survives a
# registry ever being swapped (tests construct a fresh AgentRegistry per
# case) without silently skipping the SECOND registry's own wiring, and
# without holding a strong ref that would keep an old registry alive past
# its own test's teardown.
_REMOVE_LISTENER_WIRED: "weakref.WeakSet" = weakref.WeakSet()


def _ensure_remove_listener_wired(registry) -> None:
    """#5146: subscribe :meth:`SurfaceRegistry.remove` to ``registry``'s own
    purge notification, exactly once per registry instance.

    Closes the #5084-class name-reuse hole for OPERATOR-DRIVER-TOKEN
    identity (that fix closed the same class for spawn lineage): a purge
    frees the agent name for immediate re-declaration, but
    ``SurfaceRegistry`` keeps a purged name's ``SurfaceManager`` (with its
    stale ``_active_driver``/surface set) forever without this — a
    same-name re-declare's first connection would silently inherit the
    OLD identity's operator authority. This module calls INTO
    ``registry.add_remove_listener`` (already imports ``AgentRegistry``
    indirectly via ``get_registry``); ``AgentRegistry`` itself never
    imports or calls into transport (#5139's own layering ruling) — the
    listener is the seam, wired from the transport side."""
    if registry in _REMOVE_LISTENER_WIRED:
        return
    registry.add_remove_listener(surface_registry().remove)
    _REMOVE_LISTENER_WIRED.add(registry)


def _ensure_fail_close_driver(agent_name: str, manager: SurfaceManager, registry) -> None:
    """Start (or restart) the per-agent fail-close / liveness driver.

    A single background task per agent ticks the surface manager: it sweeps dead
    (heartbeat-timeout) surfaces and, when the grace window elapses with ZERO
    answerable operator surfaces, typed-DENYs the session's still-pending
    interventions (scoped per-intervention on the session side). It stops after
    firing with no surfaces; a fresh attach restarts it.
    """
    existing = _DRIVERS.get(agent_name)
    if existing is not None and not existing.done():
        return
    _DRIVERS[agent_name] = asyncio.create_task(
        _drive_fail_close(agent_name, manager, registry)
    )


async def _drive_fail_close(agent_name: str, manager: SurfaceManager, registry) -> None:
    poll = max(0.5, min(manager.grace_seconds, manager.liveness_timeout) / 4.0)
    try:
        while True:
            await asyncio.sleep(poll)
            now = monotonic()
            manager.sweep_dead(now)
            if not manager.should_fail_close(now):
                continue
            try:
                # #3793 stage 2: resolve/boot the session WITHOUT touching
                # registry's own AttachedConnection (the local TUI's shared
                # focus pointer) — AG-UI never reads attached_name/
                # attached_session (measured: 0 references), so the only
                # thing this call ever needed from attach() was the boot
                # side effect. Applies to every registry.attach(agent_name)
                # call in this file (5 sites, all changed the same way).
                session = await registry.ensure_running(agent_name)
            except Exception:  # noqa: BLE001 — session gone: nothing to deny
                session = None
            if session is not None:
                denied = await session.fail_close_interventions(NO_SURFACE_REFUSAL_REASON)
                if denied:
                    logger.info(
                        "agui: fail-close DENY'd %d pending intervention(s) for "
                        "%r (last surface lost, grace elapsed)",
                        len(denied), agent_name,
                    )
            # Grace consumed; stop until a surface reattaches (re-ensures the driver).
            return
    except asyncio.CancelledError:
        raise


def session_backlog_frames(registry, name: str, sid: str) -> "list[Frame]":
    """A session's scrollback, projected to the wire ``Frame`` shape (#3310 N3).

    Projects through the SAME restore projection (resolved-only,
    tool-coalesced) local restore-on-restart uses (#3273 Phase 5) — one shared
    SSoT, not a second display-shaping implementation. ``session.history`` is
    the in-memory ``ChatMessage`` log ``load_history()`` populated from
    ``history.jsonl`` at session construction — authoritative-at-read, not
    WAL-derived (see ``restore.py``'s own recovery-gate note). Used both as
    this endpoint's ``AgUiEmitter`` backlog-provider (a switch re-fire) and
    directly by tests to build the "what a local hydrate would show" oracle.
    """
    target = registry.get_session(name, sid)
    if target is None:
        return []
    history = list(getattr(target, "history", []) or [])
    return [DisplayFrame(m) for m in project_restored_frames(history)]


def session_backlog_page(
    registry, name: str, sid: str, *, before_root_id: "str | None" = None,
) -> "tuple[list[Frame], bool, str | None]":
    """#5139 C: ONE bounded backlog page — at most :data:`HYDRATE_PAGE_FRAMES`
    frames, cut only at a turn (``chain_id``) boundary. Unlike
    :func:`session_backlog_frames` (unbounded, still used as the "what a
    local hydrate would show" test oracle and the switch-follow re-fire's
    own established shape), this is the one both the initial connect/switch
    backlog AND a client-driven ``ReachedTop`` pull now go through — server
    sends at most one page per request (architect's own six-questions⑤
    answer: the bound's OWNER is the server), continuation is client-pull,
    never a second server-initiated push.

    Returns ``(frames, has_more, next_cursor)`` — see
    :func:`~reyn.interfaces.inline.textual_chat.restore.page_restored_history`,
    which this wraps, for what each means."""
    target = registry.get_session(name, sid)
    if target is None:
        return [], False, None
    history = list(getattr(target, "history", []) or [])
    frames, has_more, next_cursor = page_restored_history(
        history, before_root_id=before_root_id, limit=HYDRATE_PAGE_FRAMES,
    )
    return [DisplayFrame(m) for m in frames], has_more, next_cursor


class _SessionFrameSource:
    """Per-connection unified frame stream off a session (server analogue of
    :class:`InProcessTransport`): fan out ``session.outbox`` as DisplayFrames and
    the renderer-relevant ``session.audit_events`` subset as EventFrames onto one
    ordered queue.

    **Session-switch follow (#3310 N3, ported #4534 PR-2b).** This source is
    bound to ONE session object at construction, but a remote client can
    switch which of the agent's sessions it is viewing (``/session switch
    <sid>``, now ``ClientTransport.request_session_switch`` →
    ``registry.attach_session`` — see ``interfaces/slash/session.py``).
    ``attach_session`` calls ``registry`` directly, out-of-band from this
    source's own ``session.outbox_hub`` subscription (unlike the retired
    ``__session_switch_request__`` sentinel, which arrived IN-BAND on that
    same hub). So this source instead SUBSCRIBES to the switch — when
    ``registry`` + ``agent_name`` are supplied, :meth:`_bind`'s caller
    registers :meth:`_on_attach_announced` via
    ``registry.add_attach_listener`` — and reacts by re-pointing itself at
    the target session (``registry.get_session``) and synthesizing the SAME
    ``session_attached`` audit-event #3310 N1 emits on ``repl_outbox`` — the
    barrier the emitter (``AgUiEmitter``) uses to re-fire the reconnect
    protocol for the new session (its ``backlog_provider``). This is a
    PARALLEL, independent reaction to the switch the registry's own
    ``attach_session`` already performed — it never calls
    ``registry.attach_session`` itself, so it cannot race or double-apply
    that side effect; it only re-points THIS connection's own view. A
    registry-less / agent_name-less construction (as most existing unit
    tests build this class) degrades to no switch-follow at all — no
    listener is registered, so nothing but the ordinary ``DisplayFrame`` /
    ``EventFrame`` stream ever reaches :attr:`_q`.

    **The listener fires synchronously** (``registry``'s own no-await
    critical section — see ``add_attach_listener``'s docstring), so it
    cannot itself do the re-point (that needs ``await sub.close()``-adjacent
    bookkeeping and races the in-flight ``await sub.get()`` this source's
    drain loop is blocked in). It only hands the sid to
    :attr:`_switch_signal`, an ``asyncio.Queue`` :meth:`_drain_one_session`
    dual-waits alongside ``sub.get()`` (``asyncio.wait(...,
    return_when=FIRST_COMPLETED)``) — the second wait source a blocked
    in-flight await needs to be interrupted by an out-of-band signal.

    ★No per-client "which frames has this connection already seen"
    bookkeeping (design constraint, #3310 issue thread — rejected as state
    that has to be kept correct forever). The switch-follow above is
    re-subscription only: WHICH session's ``outbox_hub``/``audit_events`` this
    source currently reads from (:attr:`_session`, replaced wholesale on a
    switch), plus a FRESH read of that session's live ``history`` at
    switch-time (the emitter's ``backlog_provider``) — never a set of
    previously-delivered frame or message ids consulted before forwarding.
    A future change that needs to track "have I already sent X" to this
    connection is out of scope for this mechanism; do not bolt it on here —
    it would reintroduce exactly the state class this design avoided.

    ★#5116 (architect ruling: "lifting state up" / "unidirectional" / "no
    derived state" — the react vocabulary the owner asked for). This
    instance is now THE single per-connection owner of "which agent, which
    session" — :attr:`_agent_name`/:attr:`_session` are the ONE copy;
    :meth:`current_agent_name`/:meth:`current_session` are the ONE read
    path (``_status_provider`` calls these fresh each time now, never
    closes over a session variable frozen at connect time — the "derived
    state" antipattern the owner named). The announce this class emits
    NAMES WHATEVER IT WAS JUST TOLD TO BECOME (never ``self._agent_name``
    read-then-written-back — see :meth:`_drain_one_session`'s own comment
    at the fix site) — unidirectional: told, not self-referencing.

    **What #5116 actually closes**: :meth:`add_attach_listener` is keyed
    by AGENT NAME — correct for "a session-switch within the SAME agent
    this connection is already watching" (the mechanism above), but
    structurally unable to notify a connection about attaching to a
    DIFFERENT agent (nobody is ever listening under the NEW agent's own
    key, because no connection is open to ITS url). A connection's own
    cross-agent ``/attach`` is therefore signalled through a SEPARATE,
    connection-id-keyed channel (:data:`_CONNECTION_RETARGET_HUB`, module
    level below) — the ``attach_request`` handler (a DIFFERENT HTTP
    request than this SSE stream, correlated only by ``connection_id``)
    notifies it directly. Both channels feed the SAME
    :attr:`_switch_signal` queue and the SAME re-point/announce code in
    :meth:`_drain_one_session` — one mechanism, two ways to trigger it,
    not two mechanisms (#5116's own "stop duplicating" ruling, applied
    here rather than inventing a parallel re-point implementation)."""

    def __init__(self, session, *, registry=None, agent_name: str = "") -> None:
        self._registry = registry
        self._agent_name = agent_name
        self._q: "asyncio.Queue[Frame]" = asyncio.Queue()
        self._forward = forwarded_frame_kinds()
        self._drain_task: "asyncio.Task | None" = None
        self._sub = None
        self._session = None
        self._events = None
        # #4534 PR-2b (widened #5116): the switch-follow signal — now
        # carries (agent_name, sid) rather than a bare sid, so the SAME
        # queue/dual-wait/re-point code in _drain_one_session serves BOTH
        # a same-agent session-switch (agent_name == self._agent_name,
        # registry.add_attach_listener feeds it) AND a cross-agent
        # /attach (agent_name == the NEW target, _CONNECTION_RETARGET_HUB
        # feeds it) — one mechanism, two producers, not two mechanisms.
        self._switch_signal: "asyncio.Queue[tuple[str, str]]" = asyncio.Queue()
        self._listening_for = ""
        self._connection_id = ""
        self._bind(session)
        if self._registry is not None and self._agent_name:
            self._registry.add_attach_listener(self._agent_name, self._on_attach_announced)
            self._listening_for = self._agent_name

    def current_agent_name(self) -> str:
        """#5116: the ONE read path for "which agent is this connection
        looking at right now" — ``_status_provider`` calls this fresh on
        every status read instead of closing over a name frozen at
        connect time (the "derived state" antipattern, owner's own
        framing)."""
        return self._agent_name

    def current_session(self):
        """#5116: the ONE read path for "which session is this connection
        looking at right now" — see :meth:`current_agent_name`."""
        return self._session

    def listen_for_retarget(self, connection_id: str) -> None:
        """#5116: subscribe this source to :data:`_CONNECTION_RETARGET_HUB`
        under ``connection_id`` — the cross-agent counterpart to
        ``registry.add_attach_listener`` above (agent-keyed, same-agent
        session-switch only). Called once, right after construction, by
        the route handler that already knows this connection's id."""
        self._connection_id = connection_id
        _CONNECTION_RETARGET_HUB.subscribe(
            connection_id, self._on_retarget_announced, self._agent_name,
        )

    def _on_retarget_announced(self, agent_name: str, sid: str) -> None:
        """Hub callback (#5116) — synchronous, no ``await``: mirrors
        :meth:`_on_attach_announced`'s own idiom, feeding the SAME queue
        with an explicit ``agent_name`` (the cross-agent case cannot
        assume ``self._agent_name`` — that is precisely the value being
        replaced)."""
        self._switch_signal.put_nowait((agent_name, sid))

    def _bind(self, session) -> None:
        """Point this source at ``session``'s own audit-event stream (the
        outbox-hub subscription is (re)established per drain iteration,
        see :meth:`_drain_outbox`)."""
        self._session = session
        self._events = getattr(session, "audit_events", None) or getattr(
            session, "_audit_events", None
        )
        if self._events is not None:
            self._events.add_subscriber(self._on_audit_event)

    def _unbind(self, session) -> None:
        events = getattr(session, "audit_events", None) or getattr(
            session, "_audit_events", None
        )
        if events is not None:
            events.remove_subscriber(self._on_audit_event)

    def _on_audit_event(self, event) -> None:
        if getattr(event, "type", None) in self._forward:
            self._q.put_nowait(EventFrame(event))

    def _on_attach_announced(self, sid: str) -> None:
        """Registry callback (#4534 PR-2b) — synchronous, no ``await``: hand
        ``(self._agent_name, sid)`` to :attr:`_switch_signal` and return,
        mirroring :meth:`_on_audit_event`'s own idiom. Same-agent only (the
        registry keys this listener by ``self._agent_name`` itself, #5116's
        own finding) — the cross-agent counterpart is
        :meth:`_on_retarget_announced`."""
        self._switch_signal.put_nowait((self._agent_name, sid))

    def start(self) -> None:
        self._drain_task = asyncio.create_task(self._drain_outbox())

    def close(self) -> None:
        self._unbind(self._session)
        if self._registry is not None and self._listening_for:
            self._registry.remove_attach_listener(self._listening_for, self._on_attach_announced)
        if self._connection_id:
            _CONNECTION_RETARGET_HUB.unsubscribe(self._connection_id, self._on_retarget_announced)
        if self._sub is not None:
            self._sub.close()
        if self._drain_task is not None:
            self._drain_task.cancel()

    def _resolve_switch_target(self, agent_name: str, sid: str):
        """The target session for a switch-follow signal, or ``None`` when
        this source is registry-less (no switch-follow), the sid names no
        loaded session, or the target is already the current one (a no-op
        switch/retarget). #5116: ``agent_name`` is now an explicit
        parameter (the signal's own payload) rather than always
        ``self._agent_name`` — a cross-agent retarget names a DIFFERENT
        agent than this source is currently bound to; a same-agent
        session-switch happens to pass the same name it already had."""
        if self._registry is None or not sid:
            return None
        target = self._registry.get_session(agent_name, sid)
        if target is None or target is self._session:
            return None
        return target

    async def _drain_outbox(self) -> None:
        # ADR-0039 P6b: subscribe to the session's outbox *hub* (a bounded
        # per-surface queue) instead of draining ``session.outbox`` directly.
        # This surface therefore receives the FULL stream even when other AG-UI
        # / local surfaces are attached (asyncio.Queue's single-getter steal is
        # resolved by the hub's single-drain fan-out). A stuck SSE reader is
        # disconnect-slow'd by the hub — ``get()`` then returns ``None`` and we
        # end this surface's stream with a synthetic terminal frame.
        #
        # #3310 N3: the outer loop re-subscribes to a NEW session's hub after
        # a switch (the inner loop returns ``True``); a genuine end/disconnect
        # returns ``False`` and this task exits.
        while True:
            self._sub = self._session.outbox_hub.subscribe(maxsize=DEFAULT_SURFACE_MAXSIZE)
            switched = await self._drain_one_session()
            if not switched:
                return

    async def _drain_one_session(self) -> bool:
        """Drain ``self._sub`` (the current session's outbox_hub
        subscription) until it ends or a switch-follow signal re-points this
        source at a different session.

        #4534 PR-2b: dual-waits ``sub.get()`` alongside
        ``self._switch_signal.get()`` (``asyncio.wait(...,
        return_when=FIRST_COMPLETED)``) — the switch signal now arrives
        OUT-OF-BAND (a registry callback, not a message on this same
        subscription), so the task genuinely blocked in ``await sub.get()``
        needs a second wait source to be interrupted by it. ``asyncio.wait``
        does not cancel the loser; a signal that arrives after ``sub.get()``
        already won stays queued for the next iteration (no message loss on
        that side). The narrow remaining race is the reverse: if BOTH
        complete in the same tick, the switch is processed first and the
        already-fetched outbox message is dropped rather than requeued —
        accepted (documented, not silently absorbed): the two are produced
        by independent event-loop callbacks (the hub's fan-out vs. the
        registry's synchronous notify), so simultaneous readiness needs both
        callbacks to run in the same iteration, which a real switch (an
        operator-paced action) essentially never coincides with a message
        arriving in the exact same tick.
        """
        sub = self._sub
        get_task: "asyncio.Task | None" = None
        switch_task: "asyncio.Task | None" = None
        try:
            while True:
                if get_task is None:
                    get_task = asyncio.create_task(sub.get())
                if switch_task is None:
                    switch_task = asyncio.create_task(self._switch_signal.get())
                done, _ = await asyncio.wait(
                    {get_task, switch_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if switch_task in done:
                    signal_agent_name, sid = switch_task.result()
                    switch_task = None
                    target = self._resolve_switch_target(signal_agent_name, sid)
                    if target is not None:
                        if get_task is not None and not get_task.done():
                            get_task.cancel()
                        get_task = None
                        old_agent_name = self._agent_name
                        old_session = self._session
                        self._unbind(old_session)
                        sub.close()
                        # #5116: THIS is the fix site. self._agent_name is
                        # updated to the signal's own target BEFORE the
                        # announce is built — "told, not self-referencing"
                        # (architect's "unidirectional" ruling). The
                        # pre-#5116 bug: this line did not exist, so a
                        # cross-agent retarget's announce below still read
                        # self._agent_name == the OLD (connection-opening)
                        # agent, teaching the client to re-hydrate as the
                        # wrong agent even though the session object itself
                        # (``target``) was already correctly resolved.
                        self._agent_name = signal_agent_name
                        # Same-agent-switch-follow re-registration: if the
                        # agent changed, this connection must now listen
                        # for FUTURE same-agent switches under the NEW key
                        # (the OLD key's listener would fire on the wrong
                        # agent's switches from here on).
                        if (
                            self._registry is not None
                            and old_agent_name != signal_agent_name
                            and self._listening_for
                        ):
                            self._registry.remove_attach_listener(
                                self._listening_for, self._on_attach_announced,
                            )
                            self._registry.add_attach_listener(
                                signal_agent_name, self._on_attach_announced,
                            )
                            self._listening_for = signal_agent_name
                        # ★Barrier ordering (co-vet #3310 N3 (a)): the announce
                        # is enqueued BEFORE ``_bind(target)`` makes the new
                        # session's audit-event subscriber live. ``_bind`` calls
                        # ``add_subscriber`` synchronously, and ``_on_audit_event``
                        # is itself synchronous (``_q.put_nowait`` — no await),
                        # so an audit-event the new session emits CANNOT reach
                        # ``_q`` before its subscriber exists. Emitting the
                        # announce first, THEN subscribing, therefore makes
                        # "barrier before any of the new session's own frames"
                        # hold BY CONSTRUCTION regardless of whether an ``await``
                        # is ever later introduced between the two steps — not
                        # merely true today because there happens to be none
                        # (the SAME barrier property N1 built for
                        # ``AgentRegistry.attach``/``attach_session``, applied
                        # here to the ORDER of two synchronous calls rather than
                        # a flip + a queue put). The announce payload now reads
                        # ``self._agent_name`` AFTER the update two lines above
                        # — #5116: this is the ONE place the connection's own
                        # "which agent" fact is written, and every other reader
                        # (announce, status, frame source) derives from it.
                        self._q.put_nowait(
                            EventFrame(
                                Event(
                                    type="session_attached",
                                    data={"agent": self._agent_name, "session_id": sid},
                                )
                            )
                        )
                        self._bind(target)
                        return True
                    continue  # unresolved / no-op signal: drop silently, keep draining
                if get_task in done:
                    msg = get_task.result()
                    get_task = None
                    if msg is None:
                        self._q.put_nowait(DisplayFrame(OutboxMessage(kind="__end__", text="")))
                        return False
                    self._q.put_nowait(DisplayFrame(msg))
                    if msg.kind == "__end__":
                        return False
        finally:
            if get_task is not None and not get_task.done():
                get_task.cancel()
            if switch_task is not None and not switch_task.done():
                switch_task.cancel()

    async def frames(self):
        while True:
            frame = await self._q.get()
            # #3570, the server-side instance of the same shape as
            # ``InProcessTransport.frames``: ``_q`` is fed by SYNCHRONOUS
            # ``put_nowait`` callers (the audit-event subscriber, the forwarder),
            # so a burst leaves it non-empty and ``get()`` stops suspending —
            # the emitter would then encode + serialize the whole burst without
            # the server's event loop running anything else (other connections'
            # writes, the fail-close driver's timers).
            await suspend_between_frames()
            yield frame
            if isinstance(frame, DisplayFrame) and frame.message.kind == "__end__":
                return


@router.get("/agui/chat/{agent_name}/events")
async def agui_events(request: Request, agent_name: str):
    """SSE stream of the session's frames as AG-UI events (server→client).

    On connect the surface is attached to the per-agent :class:`SurfaceManager`
    (Axis-B active-driver + fail-close liveness) and, when it is the first
    surface, the operator intervention listener is registered so an ``ask_user``
    reaches this remote operator. On disconnect the surface detaches; when it was
    the last, the listener is unregistered and the grace window arms.
    """
    auth = _auth_context(request)
    if auth is None:
        return JSONResponse({"error": "authentication unavailable"}, status_code=401)
    connection_id = _connection_id_from_request(request)
    identity = authenticate_request(request, auth, connection_id=connection_id)
    if not identity.authenticated:
        return JSONResponse({"error": "authentication required"}, status_code=401)

    registry = get_registry()
    _ensure_remove_listener_wired(registry)
    if not registry.exists(agent_name):
        return JSONResponse({"error": f"agent {agent_name!r} not found"}, status_code=404)
    session = await registry.ensure_running(agent_name)  # #3793 stage 2: boot-only, does not touch focus

    manager = _surface_manager(agent_name, auth)
    now = monotonic()
    first = not manager.has_surfaces()
    manager.attach(connection_id, identity.user_id, now)
    if first:
        session.register_intervention_listener(AGUI_OPERATOR_CHANNEL)
    session.emit_audit_event(
        "client_attached",
        auth_user_id=identity.user_id,
        auth_connection_id=connection_id,
        auth_tier=identity.tier.value,
    )
    _ensure_fail_close_driver(agent_name, manager, registry)

    source = _SessionFrameSource(session, registry=registry, agent_name=agent_name)
    # #5119 (architect co-vet on #5118, issuecomment-5380501066, item ④):
    # everything from here through the ``AgUiEmitter`` construction below
    # runs BEFORE ``gen()``'s own ``try``/``finally`` (which is where
    # ``source.close()`` normally lives) ever starts — Starlette does not
    # begin pulling from ``gen()`` until AFTER this function returns. An
    # exception raised anywhere in this window (``listen_for_retarget``'s
    # own hub subscription, ``source.start()``'s background task, either
    # backlog fetch, ``AgUiEmitter`` construction) used to leave
    # ``source`` un-closed forever — no ``finally`` was ever reached to
    # call it. For the registry-keyed listener
    # (``registry.add_attach_listener``) this is bounded by agent count;
    # for :data:`_CONNECTION_RETARGET_HUB` (#5116, keyed by
    # ``connection_id``) it is NOT — a fresh key every connection, no
    # natural ceiling. Wrapping this whole window in its own try/except
    # closes the CLASS of hole (every exception site in this span), not
    # just the one call ``listen_for_retarget`` that happened to be named
    # in the finding.
    try:
        source.listen_for_retarget(connection_id)  # #5116: cross-agent /attach
        source.start()

        def _status_provider():
            # #5094: read status off THIS connection's own resolved session
            # rather than ``_snapshot(registry)``, which reads the registry's
            # single GLOBAL attached-pointer — deliberately never set for an
            # AG-UI connection (``ensure_running``'s own #3793-stage-2 docstring)
            # — and so returned ``None`` wholesale on every first connect,
            # regardless of what #5097/#5104 already fixed inside that dict. See
            # ``_snapshot_for_session``'s own docstring for the full reasoning.
            #
            # #5116: reads ``source.current_session()`` FRESH on every call,
            # never the ``session`` local captured above — that capture is
            # frozen at connect time (this closure's own "derived state"
            # antipattern, owner's framing) and never updates after a
            # cross-agent ``/attach`` re-points ``source`` to a different
            # session. ``source`` is the SINGLE per-connection owner of
            # "which agent, which session" now (see its own class docstring);
            # this is its one status-facing read path.
            return _snapshot_for_session(registry, source.current_session())

        def _backlog_provider(name: str, sid: str) -> "tuple[list[Frame], bool, str | None]":
            return session_backlog_page(registry, name, sid)

        # #3328: the INITIAL connect's `MESSAGES_SNAPSHOT` was silently empty —
        # `backlog_provider` (above) is only ever CALLED off a mid-stream
        # `session_attached` sentinel (#3310 N3's switch re-fire); a first-time
        # connect never triggers that sentinel, so `AgUiEmitter._backlog`
        # defaulted to `[]` and a `--connect` to an agent with existing
        # conversation history (produced locally, or by an earlier remote
        # session, before this connection attached) rendered NO scrollback at
        # all — contradicting this endpoint's own doc
        # (`agui-transport.md` "## Reconnect": "On connect (or reconnect) the
        # server replays... MESSAGES_SNAPSHOT... so a reconnecting client
        # rebuilds its scrollback"). `attach(agent_name)` always focuses
        # `_DEFAULT_SID` (registry.py's `attach`), so the initial backlog is that
        # session's own history through the SAME projection the switch re-fire
        # and the local restore-on-restart path both use — one SSoT, populated
        # at both call sites now instead of only the second.
        initial_backlog, initial_has_more, initial_next_cursor = session_backlog_page(
            registry, agent_name, _DEFAULT_SID,
        )
        emitter = AgUiEmitter(
            source.frames(), _status_provider,
            backlog=initial_backlog,
            backlog_has_more=initial_has_more,
            backlog_next_cursor=initial_next_cursor,
            backlog_provider=_backlog_provider,
        )
    except Exception:
        source.close()
        raise

    async def gen():
        try:
            async for chunk in emitter.stream():
                yield chunk
        finally:
            source.close()
            now2 = monotonic()
            manager.detach(connection_id, now2)
            if not manager.has_surfaces():
                session.unregister_intervention_listener(AGUI_OPERATOR_CHANNEL)
                _ensure_fail_close_driver(agent_name, manager, registry)
            session.emit_audit_event(
                "client_detached",
                auth_user_id=identity.user_id,
                auth_connection_id=connection_id,
            )

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _handle_answer(request, auth, identity, connection_id, agent_name, payload):
    """TOOL_CALL_RESULT → deliver BY ID (R1) through the single funnel.

    Delivery-time authorization (the client is UNTRUSTED): re-check
    ``authorize_write`` and the active-driver token here — a deposed holder's late
    answer is rejected at this seam (seize↔answer race). Then resolve the
    intervention BY the echoed ``toolCallId``; the server validates the id (and any
    ``choiceId``) against its OWN registry entry — the client's prompt copy is not
    trusted (R6).
    """
    if not auth.authorize_write(identity):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    manager = surface_registry().get(agent_name)
    if manager is not None and not manager.is_active_driver(connection_id):
        return JSONResponse(
            {"error": "not the active driver", "answered": False}, status_code=409
        )
    iv_id = str(payload.get("toolCallId") or "").strip()
    if not iv_id:
        return JSONResponse({"error": "missing toolCallId", "answered": False}, status_code=400)
    session = await get_registry().ensure_running(agent_name)  # #3793 stage 2: boot-only, does not touch focus
    if manager is not None:
        manager.heartbeat(connection_id, monotonic())
    choice_id = payload.get("choiceId")
    attribution = {
        "auth_user_id": identity.user_id,
        "auth_connection_id": connection_id,
    }
    answered = await session.answer_intervention_by_id(
        iv_id,
        str(payload.get("text", "")),
        choice_id_override=str(choice_id) if choice_id is not None else None,
        external_source=False,  # authenticated human operator = unfenced (keystone)
        attribution=attribution,
    )
    if not answered:
        # Unknown / already-resolved id, or a choice that failed server-side
        # validation — a typed reject, NO head fallback (R1).
        return JSONResponse(
            {"answered": False, "reason": "no matching pending intervention for id"},
            status_code=409,
        )
    return JSONResponse({"answered": True})


@router.post("/agui/chat/{agent_name}/seize")
async def agui_seize(request: Request, agent_name: str):
    """Symmetric, auth-gated seize of the active-driver token (D4).

    Any authorized attached surface may seize equally (no handshake). Refused for
    an unauthenticated connection (Axis-A), an unauthorized identity, or a
    connection with no attached surface. Emits ``client_seized`` attribution.
    """
    auth = _auth_context(request)
    if auth is None:
        return JSONResponse({"error": "authentication unavailable"}, status_code=401)
    connection_id = _connection_id_from_request(request)
    identity = authenticate_request(request, auth, connection_id=connection_id)
    if not identity.authenticated:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    # #5129 (architect, scope widened issuecomment-5382969115): this POST is,
    # same as agui_submit, a DIFFERENT HTTP request than the SSE stream it
    # seizes driver authority ON BEHALF OF — resolve from the connection,
    # never the URL's own path param, or a post-attach seize grabs the
    # ORIGINAL agent's surface instead of the one this connection is
    # actually attached to now.
    agent_name = connection_current_agent(connection_id) or agent_name
    manager = surface_registry().get(agent_name)
    if manager is None or not manager.seize(connection_id, identity.user_id, monotonic()):
        return JSONResponse({"seized": False, "error": "seize refused"}, status_code=409)
    registry = get_registry()
    _ensure_remove_listener_wired(registry)
    if registry.exists(agent_name):
        session = await registry.ensure_running(agent_name)  # #3793 stage 2: boot-only, does not touch focus
        session.emit_audit_event(
            "client_seized",
            auth_user_id=identity.user_id,
            auth_connection_id=connection_id,
        )
    return JSONResponse({"seized": True})


@router.post("/agui/chat/{agent_name}")
async def agui_submit(request: Request, agent_name: str):
    """Client→server: turn submit, HITL answer, cancel, and heartbeat.

    Server-side actions only (A3): a client may submit a turn / answer / cancel /
    keepalive — it may NEVER shut the single-writer server down (there is no
    shutdown verb here).
    """
    auth = _auth_context(request)
    if auth is None:
        return JSONResponse({"error": "authentication unavailable"}, status_code=401)
    connection_id = _connection_id_from_request(request)
    identity = authenticate_request(request, auth, connection_id=connection_id)
    if not identity.authenticated:
        return JSONResponse({"error": "authentication required"}, status_code=401)

    # #5129: this POST is a DIFFERENT HTTP request than this connection's SSE
    # stream, correlated only by ``connection_id`` — the URL's own
    # ``agent_name`` is the agent this connection FIRST connected to and
    # never updates after that (the client's own long-lived SSE URL), while
    # ``_SessionFrameSource`` (the SSE stream's per-connection owner, #5116)
    # is the one thing a cross-agent ``/attach`` actually re-points. Every
    # decision below this line — the two heartbeat touches, ``exists``,
    # ``ensure_running``, the TOOL_CALL_RESULT delegation, and the 3
    # client-names-an-operation branches further down — reads THIS resolved
    # value, never the raw path param again, so a stale attach cannot leave
    # one of them still addressing the connection's original agent (the
    # "sends but doesn't render" symptom a partial fix would reproduce in a
    # different shape). Falls back to the URL when the hub has never seen
    # this connection_id (no SSE stream subscribed under it — a POST that
    # outraces its own connect, or a test driving this endpoint directly).
    agent_name = connection_current_agent(connection_id) or agent_name

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    ptype = payload.get("type")

    # Heartbeat fast-path (liveness): a pure in-memory refresh of the surface's
    # keepalive timestamp, deliberately dispatched BEFORE ``registry.exists()``
    # below — that call is a filesystem stat (``Path.is_file()`` on the agent's
    # profile). An already-attached connection is tracked in the in-process
    # SurfaceManager; its liveness ping needs no re-verification that the agent
    # profile still exists on disk. The auth gate above still applies (a
    # heartbeat requires an authenticated connection) — only the disk stat is
    # skipped. An unattached / unknown connection's heartbeat is a harmless
    # no-op (``manager`` or the surface lookup misses; no side effect).
    if ptype == "heartbeat":
        manager = surface_registry().get(agent_name)
        if manager is not None:
            manager.heartbeat(connection_id, monotonic())
        return JSONResponse({"status": "ok"})

    registry = get_registry()
    _ensure_remove_listener_wired(registry)
    if not registry.exists(agent_name):
        return JSONResponse({"error": f"agent {agent_name!r} not found"}, status_code=404)

    # Liveness refresh for non-heartbeat traffic too (piggyback): any accepted
    # POST counts as proof of life, so the client's own dedicated heartbeat
    # loop can skip its ping when real traffic already refreshed this.
    manager = surface_registry().get(agent_name)
    if manager is not None:
        manager.heartbeat(connection_id, monotonic())

    # HITL answer (R1 by-id) — delivery-time authorized.
    if ptype == "TOOL_CALL_RESULT":
        return await _handle_answer(
            request, auth, identity, connection_id, agent_name, payload
        )

    # Turn submit / cancel are permission-gated writes (server actions).
    if not auth.authorize_write(identity):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    session = await registry.ensure_running(agent_name)  # #3793 stage 2: boot-only, does not touch focus
    if ptype == "user_message":
        text = str(payload.get("text", "")).strip()
        if text:
            # ADR-0039 multi-client input-broadcast fix: attribute this
            # submit the same way `_handle_answer` attributes a HITL grant
            # (auth_user_id + connection id) — `submit_user_text` emits a
            # `user_submitted` audit-event (#3300 P1 C) that every attached
            # surface's event→display handler renders as this client's turn,
            # not just the agent's reply.
            msg_id = await session.submit_user_text(
                text,
                attribution={
                    "auth_user_id": identity.user_id,
                    "auth_connection_id": connection_id,
                },
            )
            # #3287: echo the assigned msg_id back to the SUBMITTING client —
            # the SAME correlation id the broadcast user_submitted event above
            # carries — so it can recognise its own echo BY ID (never a
            # same-text match, which a second client submitting an identical
            # line would defeat) and skip re-rendering a line its own input
            # surface already showed. See `AgUiTransport.submit_user_text` /
            # `remote_client.py`'s `send`.
            return JSONResponse({"status": "ok", "msg_id": msg_id})
    elif ptype == "slash_command":
        # #3595 S5: the REMOTE half of the shared client-side slash layer. The
        # client interpreted the operator's line and resolved it against its own
        # registry; what arrives here is a command NAME plus its argument
        # string, so nothing on the server side tests a leading ``/``. The name
        # is re-resolved against THIS process's registry — a client on a
        # different build must not be able to name something this one does not
        # have — and an unknown name answers ``ran: False`` rather than raising.
        #
        # A remote client holds no ``Session``, so eleven of the registered
        # commands (the S4 residue: /model, /cost, /image, …) can only run where
        # the session is. Executing them here is what keeps a ``--connect``
        # attach's slash catalog identical to a local one; it rides the same
        # ``authorize_write`` gate above that a turn submit does, which is the
        # gate they already passed when they rode ``user_message``.
        from reyn.interfaces.slash.dispatch import execute_slash_command
        name = str(payload.get("name", "")).strip()
        if name:
            ran = await execute_slash_command(
                session._slash_context(), name, str(payload.get("args", "")),
            )
            return JSONResponse({"status": "ok", "ran": ran})
        return JSONResponse({"status": "ok", "ran": False})
    elif ptype == "cancel_inflight":
        cancel_fn = getattr(session, "cancel_inflight", None)
        if callable(cancel_fn):
            await cancel_fn()
    elif ptype == "cancel_queued":
        # #3300 P3 (Y-server): cancel-by-id for an UNDISPATCHED queued
        # message — a DISTINCT op from `cancel_inflight` above (targets the
        # inbox queue, not the running turn). The server's own atomic
        # queued/dispatched judgement (`Session.cancel_queued`) decides
        # queued->removed vs already-dispatched->no-op; idempotent, so a
        # retry (e.g. after a dropped response) is always safe.
        msg_id = payload.get("msg_id")
        cancel_queued_fn = getattr(session, "cancel_queued", None)
        if callable(cancel_queued_fn) and msg_id:
            await cancel_queued_fn(msg_id)
    elif ptype == "attach_request":
        # #4534 PR-1: the remote execution side, mirroring slash_command
        # above — a typed payload naming the target agent, re-resolved
        # against THIS process's registry (never a raw sentinel string).
        # The __attach_request__ display-channel sentinel this replaces is
        # retired (#4534 PR-2) — this is the only path now.
        target = str(payload.get("agent_name", "")).strip()
        attached = False
        if target and registry.exists(target):
            target_session = await registry.attach(target)
            attached = True

            # #5133 (architect ruling): this connection's SurfaceManager
            # registration is a SEPARATE holder from the hub notify below —
            # #5129 fixed WHICH agent name `agui_submit`/`agui_seize` ask,
            # but neither of them registers this connection as a surface of
            # the TARGET's own manager, so `/seize` 409s and a HITL answer's
            # `is_active_driver` check reads the wrong (empty, or someone
            # else's) surface set. No new semantics: a cross-agent /attach
            # IS this connection's SurfaceManager attach(target) +
            # detach(old), the same two primitives connect/disconnect
            # already use (mirrors #5118's own "one mechanism, two
            # producers, not two mechanisms" — reusing agui_events'/gen's
            # own attach/detach side effects here rather than inventing a
            # third "migrated" state).
            #
            # Order: ARRIVAL before DEPARTURE (architect, citing #3310 N3's
            # own "the announce is enqueued BEFORE _bind" barrier) — the
            # reverse would leave a window where this connection belongs to
            # NO manager, and anything arriving in that window (a seize, an
            # answer) has no surface to resolve against. Every statement
            # from `new_manager.attach` below through `old_manager.detach`
            # further down runs without an `await` — a single-threaded
            # event loop gives nothing else a chance to run in between, so
            # this connection is never observably attached to both
            # managers at once either.
            #
            # (a) driver token: NEVER carried across. `SurfaceManager.
            # attach` already encodes the right default (first surface on
            # an otherwise-empty manager takes the token; an existing
            # holder keeps it) — calling it unchanged on the target IS the
            # "arrival competes normally, never seizes" rule, not a special
            # case for it. `detach` on the old manager releases the token
            # if this connection held it (a departure, same as a genuine
            # disconnect) — regaining control post-attach is `/seize`,
            # exactly the existing path, client-decided, never automatic.
            new_manager = _surface_manager(target, auth)
            now = monotonic()
            new_first = not new_manager.has_surfaces()
            new_manager.attach(connection_id, identity.user_id, now)
            if new_first:
                target_session.register_intervention_listener(AGUI_OPERATOR_CHANNEL)
            target_session.emit_audit_event(
                "client_attached",
                auth_user_id=identity.user_id,
                auth_connection_id=connection_id,
                auth_tier=identity.tier.value,
            )
            _ensure_fail_close_driver(target, new_manager, registry)

            # (b)/(c): the OLD agent's manager treats this exactly like an
            # ordinary disconnect — no "special because it's a migration"
            # branch (architect: that would give the same end state two
            # arrival paths, only one of which gets fixed by a future
            # change). `agent_name` here is still #5129's OWN resolution
            # (the connection's agent BEFORE this attach), captured before
            # `target` shadows it below.
            # lead-coder co-vet (issuecomment-5383186154): the prior draft
            # `await`ed `registry.ensure_running(agent_name)` here — an
            # exception/cancellation crossing that await between arrival
            # (above) and this detach would leave this connection
            # registered in BOTH managers FOREVER (detach never runs), so
            # the old manager keeps counting a surface that already left;
            # if it was the last real one, grace never arms — fail-close
            # silently fails OPEN. `registry.get_session` (sync,
            # non-loading, `None` if not currently running) removes that
            # window entirely — no await between attach and detach means
            # no suspension point for a cancellation to land in. (This
            # request's own earlier, UNRELATED `session = await registry.
            # ensure_running(agent_name)` — shared by every non-heartbeat/
            # non-answer ptype, attach_request included — already booted
            # the old agent by the time execution reaches here regardless;
            # this change is about not adding a SECOND boot + a new crash
            # window on top of that, not about whether the old agent ends
            # up running.) `old_manager.detach`/`_ensure_fail_close_driver`
            # never needed a session at all.
            old_manager = surface_registry().get(agent_name)
            if old_manager is not None and old_manager is not new_manager:
                old_manager.detach(connection_id, now)
                old_session = registry.get_session(agent_name)
                if not old_manager.has_surfaces():
                    if old_session is not None:
                        old_session.unregister_intervention_listener(AGUI_OPERATOR_CHANNEL)
                    _ensure_fail_close_driver(agent_name, old_manager, registry)
                if old_session is not None:
                    old_session.emit_audit_event(
                        "client_detached",
                        auth_user_id=identity.user_id,
                        auth_connection_id=connection_id,
                    )

            # #5116 (architect co-vet, issuecomment-5380501066): this POST
            # is a DIFFERENT HTTP request than the SSE stream it needs to
            # retarget — correlated only by ``connection_id`` (the
            # caller's OWN connection, already resolved above).
            # ``registry.attach`` moved the GLOBAL pointer (a separate
            # fact, #3793 stage 2); this notifies THIS connection's own
            # per-connection owner (``_SessionFrameSource``) that ITS OWN
            # "which agent, which session" fact changed too — the fix for
            # the cross-agent case ``registry.add_attach_listener`` cannot
            # reach (see ``_ConnectionRetargetHub``'s own docstring).
            #
            # ``target_session.session_id`` — NOT a re-typed ``_DEFAULT_
            # SID`` literal (this PR's own first revision did exactly
            # that, and it was the SAME "same fact typed twice" class
            # #5116 exists to close: today `attach()` always focuses
            # `_DEFAULT_SID`, so the two literals happened to agree, but
            # nothing enforced it — a future change to attach()'s own
            # focus logic would silently desync them). Reading it off the
            # session `attach()` itself just returned is the single
            # source of truth, not a second guess at what attach() did.
            # `registry.attached_sid()`/`registry.attached_name` are
            # NEVER the right read here either — those are the GLOBAL,
            # single-connection-focus fact #3793 stage 2 deliberately
            # keeps separate from any one AG-UI connection's own state,
            # and reading it would race a DIFFERENT connection's own
            # concurrent attach.
            _CONNECTION_RETARGET_HUB.notify(
                connection_id, target, target_session.session_id,
            )
        return JSONResponse({"status": "ok", "attached": attached})
    elif ptype == "session_switch_request":
        # #4534 PR-1: mirrors attach_request above. The
        # __session_switch_request__ sentinel this replaces is retired
        # (#4534 PR-2b) — _SessionFrameSource's switch-follow now subscribes
        # to registry.add_attach_listener directly instead of consuming a
        # sentinel off the outbox (see that class's own docstring).
        target_sid = str(payload.get("session_id", "")).strip()
        switched = False
        if target_sid:
            try:
                await registry.attach_session(agent_name, target_sid)
                switched = True
            except KeyError:
                pass
        return JSONResponse({"status": "ok", "switched": switched})
    elif ptype == "artifact_list_request":
        # #4494 design C: the durable artifact-ref table's own entries for
        # *agent_name*, read server-side (never trusting a client-supplied
        # path) — the fallback a remote client's own Artifacts pane
        # consults when its live conversation view carries nothing (its
        # past turns are simply not on the wire). Mirrors
        # ``attach_request``/``session_switch_request`` above: client
        # names an operation, server executes it against ITS OWN state.
        #
        # #4601: capped at the SAME join point (``list_refs_for_agent``'s
        # own ``limit``) InProcessTransport's local path caps at too —
        # this endpoint was the ORIGINAL #4601 finding (unbounded, no
        # stat), fixed here at the one place both transports share rather
        # than as an endpoint-only patch (which would leave the TUI's
        # identical fallback broken, architect's own #4601 correction).
        from reyn.config.loader import load_config
        from reyn.data.workspace.artifact_ref import list_refs_for_agent
        # ``workspace_dir`` = <project_root>/.reyn/agents/<agent_name> — three
        # levels down, so PROJECT ROOT (what list_refs_for_agent wants, same
        # as mint_ref/resolve_ref) needs three .parent hops, not two (two
        # only reaches the .reyn directory itself — InProcessTransport's own
        # reyn_state_root() derivation, a DIFFERENT thing this handler does
        # not want).
        project_root = session.workspace_dir.parent.parent.parent
        config = load_config(project_root)
        entries, total = list_refs_for_agent(
            project_root, agent_name, limit=config.artifacts.remote_fallback_limit,
        )
        return JSONResponse({"status": "ok", "entries": entries, "total": total})
    elif ptype == "session_list_request":
        # #5099: mirrors attach_request/session_switch_request/
        # artifact_list_request above — client names an operation, server
        # executes it against ITS OWN state (never a client-supplied
        # roster). Scoped to THIS connection's own *agent_name* (baked into
        # the endpoint URL at connect time), same as
        # ``artifact_list_request``'s own project_root derivation above.
        # Reads the SAME ``session_ids``/``attached_sid`` pair
        # ``InProcessTransport.request_session_list`` reads locally — one
        # source of truth, two transports.
        sessions = [
            {"sid": sid, "attached": sid == registry.attached_sid}
            for sid in registry.session_ids(agent_name)
        ]
        return JSONResponse({"status": "ok", "sessions": sessions})
    elif ptype == "load_older_backlog_request":
        # #5139 C: mirrors attach_request/session_switch_request/
        # artifact_list_request/session_list_request above — client names
        # an operation (the turn strictly older than its own last page's
        # ``next_cursor``), server executes it against ITS OWN state (never
        # a client-supplied history slice). One page per request (the bound
        # architect's six-questions⑤ answer named the SERVER as the owner
        # of) — reuses ``encode_messages_snapshot`` byte-for-byte, so the
        # client decodes this response through the SAME ``MessagesSnapshot``
        # path a live reconnect/switch ``MESSAGES_SNAPSHOT`` already does —
        # no new wire vocabulary for this one request type.
        target_sid = str(payload.get("session_id", "")).strip() or _DEFAULT_SID
        cursor = payload.get("before_root_id")
        page, has_more, next_cursor = session_backlog_page(
            registry, agent_name, target_sid,
            before_root_id=str(cursor) if cursor else None,
        )
        event_data = encode_messages_snapshot(page, has_more=has_more, next_cursor=next_cursor).data
        return JSONResponse({"status": "ok", **event_data})
    return JSONResponse({"status": "ok"})


__all__ = [
    "router",
    "authenticate_request",
    "AGUI_OPERATOR_CHANNEL",
    "session_backlog_frames",
]
