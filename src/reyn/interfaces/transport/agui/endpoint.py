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

from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.surface import (
    SurfaceManager,
    monotonic,
    surface_registry,
)
from reyn.interfaces.transport.frames import (
    DisplayFrame,
    EventFrame,
    Frame,
    renderer_chat_events,
)
from reyn.interfaces.web.auth import AuthContext, ConnectionIdentity
from reyn.interfaces.web.deps import get_registry
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.outbox_hub import DEFAULT_SURFACE_MAXSIZE
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
                session = await registry.attach(agent_name)
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


class _SessionFrameSource:
    """Per-connection unified frame stream off a session (server analogue of
    :class:`InProcessTransport`): fan out ``session.outbox`` as DisplayFrames and
    the renderer-relevant ``session.chat_events`` subset as EventFrames onto one
    ordered queue."""

    def __init__(self, session) -> None:
        self._session = session
        self._q: "asyncio.Queue[Frame]" = asyncio.Queue()
        self._forward = renderer_chat_events()
        self._events = getattr(session, "chat_events", None) or getattr(
            session, "_chat_events", None
        )
        self._drain_task: "asyncio.Task | None" = None
        self._sub = None

    def _on_chat_event(self, event) -> None:
        if getattr(event, "type", None) in self._forward:
            self._q.put_nowait(EventFrame(event))

    def start(self) -> None:
        if self._events is not None:
            self._events.add_subscriber(self._on_chat_event)
        self._drain_task = asyncio.create_task(self._drain_outbox())

    def close(self) -> None:
        if self._events is not None:
            self._events.remove_subscriber(self._on_chat_event)
        if self._sub is not None:
            self._sub.close()
        if self._drain_task is not None:
            self._drain_task.cancel()

    async def _drain_outbox(self) -> None:
        # ADR-0039 P6b: subscribe to the session's outbox *hub* (a bounded
        # per-surface queue) instead of draining ``session.outbox`` directly.
        # This surface therefore receives the FULL stream even when other AG-UI
        # / local surfaces are attached (asyncio.Queue's single-getter steal is
        # resolved by the hub's single-drain fan-out). A stuck SSE reader is
        # disconnect-slow'd by the hub — ``get()`` then returns ``None`` and we
        # end this surface's stream with a synthetic terminal frame.
        self._sub = self._session.outbox_hub.subscribe(maxsize=DEFAULT_SURFACE_MAXSIZE)
        while True:
            msg = await self._sub.get()
            if msg is None:
                self._q.put_nowait(DisplayFrame(OutboxMessage(kind="__end__", text="")))
                return
            self._q.put_nowait(DisplayFrame(msg))
            if msg.kind == "__end__":
                return

    async def frames(self):
        while True:
            frame = await self._q.get()
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
    session = await registry.attach(agent_name)

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

    source = _SessionFrameSource(session)
    source.start()

    # ADR-0039 P3: the active-task COUNT rides the STATE_* status read-model so the
    # remote inline status bar's `task` chip reaches MAIN-bar parity. A bare
    # `_snapshot(registry)` reports task_count=0 (it takes no task_cache), and the
    # task backend's `list()` is async while the emitter's status provider is sync
    # — so we mirror the local inline CUI's `_task_poll`: a background poll updates
    # a cache the sync provider folds in. The task TREE (dropdown) is NOT wired.
    _task_count_cache = {"count": 0}

    async def _poll_task_count() -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                tasks = await registry.task_backend.list()
                active = [
                    t for t in tasks
                    if getattr(t, "status", None) not in ("done", "failed", "aborted")
                ]
                _task_count_cache["count"] = len(active)
            except Exception:  # noqa: BLE001 — a poll miss must never break the stream
                logger.debug("agui task-count poll failed", exc_info=True)

    def _status_provider():
        from reyn.interfaces.inline.app import _snapshot  # noqa: PLC0415

        snap = _snapshot(registry)
        if snap is not None:
            snap = {**snap, "task_count": _task_count_cache["count"]}
        return snap

    emitter = AgUiEmitter(source.frames(), _status_provider)

    async def gen():
        poll = asyncio.create_task(_poll_task_count())
        try:
            async for chunk in emitter.stream():
                yield chunk
        finally:
            poll.cancel()
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
    session = await get_registry().attach(agent_name)
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
        session = await registry.attach(agent_name)
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

    registry = get_registry()
    if not registry.exists(agent_name):
        return JSONResponse({"error": f"agent {agent_name!r} not found"}, status_code=404)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    ptype = payload.get("type")

    # Heartbeat (liveness): refresh the surface's keepalive so a half-open
    # connection cannot hide a dead surface. No write authority needed.
    manager = surface_registry().get(agent_name)
    if manager is not None:
        manager.heartbeat(connection_id, monotonic())
    if ptype == "heartbeat":
        return JSONResponse({"status": "ok"})

    # HITL answer (R1 by-id) — delivery-time authorized.
    if ptype == "TOOL_CALL_RESULT":
        return await _handle_answer(
            request, auth, identity, connection_id, agent_name, payload
        )

    # Turn submit / cancel are permission-gated writes (server actions).
    if not auth.authorize_write(identity):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    session = await registry.attach(agent_name)
    if ptype == "user_message":
        text = str(payload.get("text", "")).strip()
        if text:
            await session.submit_user_text(text)
    elif ptype == "cancel_inflight":
        cancel_fn = getattr(session, "cancel_inflight", None)
        if callable(cancel_fn):
            await cancel_fn()
    return JSONResponse({"status": "ok"})


__all__ = [
    "router",
    "authenticate_request",
    "AGUI_OPERATOR_CHANNEL",
]
