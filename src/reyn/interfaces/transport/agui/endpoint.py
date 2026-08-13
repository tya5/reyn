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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from reyn.core.events.events import Event
from reyn.interfaces.inline.textual_chat.restore import project_restored_frames
from reyn.interfaces.repl.status import _snapshot
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.surface import (
    SurfaceManager,
    monotonic,
    surface_registry,
)
from reyn.interfaces.transport.drain import suspend_between_frames
from reyn.interfaces.transport.frames import (
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


class _SessionFrameSource:
    """Per-connection unified frame stream off a session (server analogue of
    :class:`InProcessTransport`): fan out ``session.outbox`` as DisplayFrames and
    the renderer-relevant ``session.audit_events`` subset as EventFrames onto one
    ordered queue.

    **Session-switch follow (#3310 N3).** This source is bound to ONE session
    object at construction, but a remote client can switch which of the
    agent's sessions it is viewing (``/session switch <sid>``, the same
    ``__session_switch_request__`` sentinel ``registry._forwarder`` consumes
    for the local REPL — see ``interfaces/slash/session.py``). This source
    ALSO sees that sentinel (it fans out off ``session.outbox_hub``, the same
    hub the registry forwarder subscribes to) and, when ``registry`` +
    ``agent_name`` are supplied, reacts to it independently: it re-points
    itself at the target session (:meth:`_peek_session`'s public counterpart,
    ``registry.get_session``) and synthesizes the SAME ``session_attached``
    audit-event #3310 N1 emits on ``repl_outbox`` — the barrier the emitter
    (``AgUiEmitter``) uses to re-fire the reconnect protocol for the new
    session (its ``backlog_provider``). This is a PARALLEL, independent
    reaction to a message the registry's own forwarder already handles for
    the local REPL — it never calls ``registry.attach_session`` itself, so it
    cannot race or double-apply that side effect; it only re-points THIS
    connection's own view. A registry-less / agent_name-less construction (as
    every existing unit test builds this class) degrades to the pre-N3
    behavior byte-identically: the sentinel falls through to the generic
    ``DisplayFrame`` path, where the emitter's ``CONTROL_FILTER_KINDS``
    already silently drops it (a fail-safe, per ``protocol.py``).

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
    it would reintroduce exactly the state class this design avoided."""

    def __init__(self, session, *, registry=None, agent_name: str = "") -> None:
        self._registry = registry
        self._agent_name = agent_name
        self._q: "asyncio.Queue[Frame]" = asyncio.Queue()
        self._forward = forwarded_frame_kinds()
        self._drain_task: "asyncio.Task | None" = None
        self._sub = None
        self._session = None
        self._events = None
        self._bind(session)

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

    def start(self) -> None:
        self._drain_task = asyncio.create_task(self._drain_outbox())

    def close(self) -> None:
        self._unbind(self._session)
        if self._sub is not None:
            self._sub.close()
        if self._drain_task is not None:
            self._drain_task.cancel()

    def _resolve_switch_target(self, sid: str):
        """The target session for a ``__session_switch_request__`` sentinel,
        or ``None`` when this source is registry-less (pre-N3 degrade), the
        sid names no loaded session, or the sid is already the current one
        (a no-op switch)."""
        if self._registry is None or not sid:
            return None
        target = self._registry.get_session(self._agent_name, sid)
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
        sub = self._sub
        while True:
            msg = await sub.get()
            if msg is None:
                self._q.put_nowait(DisplayFrame(OutboxMessage(kind="__end__", text="")))
                return False
            if msg.kind == "__session_switch_request__":
                target = self._resolve_switch_target(msg.text)
                if target is not None:
                    old_session = self._session
                    self._unbind(old_session)
                    sub.close()
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
                    # a flip + a queue put). The announce payload depends
                    # only on ``self._agent_name``/``msg.text``, never on
                    # ``_bind``'s result, so reordering is free.
                    self._q.put_nowait(
                        EventFrame(
                            Event(
                                type="session_attached",
                                data={"agent": self._agent_name, "session_id": msg.text},
                            )
                        )
                    )
                    self._bind(target)
                    return True
                continue  # registry-less / unknown / no-op sid: drop silently
            self._q.put_nowait(DisplayFrame(msg))
            if msg.kind == "__end__":
                return False

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
    source.start()

    def _status_provider():
        return _snapshot(registry)

    def _backlog_provider(name: str, sid: str) -> "list[Frame]":
        return session_backlog_frames(registry, name, sid)

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
    emitter = AgUiEmitter(
        source.frames(), _status_provider,
        backlog=session_backlog_frames(registry, agent_name, _DEFAULT_SID),
        backlog_provider=_backlog_provider,
    )

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
    manager = surface_registry().get(agent_name)
    if manager is None or not manager.seize(connection_id, identity.user_id, monotonic()):
        return JSONResponse({"seized": False, "error": "seize refused"}, status_code=409)
    registry = get_registry()
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
        # against THIS process's registry (never the raw
        # __attach_request__ sentinel string). ADD-ONLY: the sentinel
        # path is unchanged and still live.
        target = str(payload.get("agent_name", "")).strip()
        attached = False
        if target and registry.exists(target):
            await registry.attach(target)
            attached = True
        return JSONResponse({"status": "ok", "attached": attached})
    elif ptype == "session_switch_request":
        # #4534 PR-1: mirrors attach_request above, retiring
        # __session_switch_request__.
        target_sid = str(payload.get("session_id", "")).strip()
        switched = False
        if target_sid:
            try:
                await registry.attach_session(agent_name, target_sid)
                switched = True
            except KeyError:
                pass
        return JSONResponse({"status": "ok", "switched": switched})
    return JSONResponse({"status": "ok"})


__all__ = [
    "router",
    "authenticate_request",
    "AGUI_OPERATOR_CHANNEL",
    "session_backlog_frames",
]
