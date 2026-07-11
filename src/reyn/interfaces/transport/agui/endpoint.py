"""FastAPI AG-UI transport endpoint — HTTP+SSE, behind the P0 auth gate (P2).

The wire surface for the remote thin client (D2): a new SSE endpoint that streams
the server session's :class:`~reyn.interfaces.transport.frames.Frame` stream as
AG-UI events (via :class:`~reyn.interfaces.transport.agui.emitter.AgUiEmitter`),
plus a POST for the client→server turn submit. It is modelled on the existing
A2A SSE pattern (``StreamingResponse`` / ``text/event-stream``) — A2A is the
internal spine (D1); this is the AG-UI UI surface, a distinct endpoint that does
NOT touch ``ws/chat`` (that consolidation is P6).

Every connection is gated by the **P0 auth context** (``app.state.auth``, merged
already): the request identity is resolved through the SAME
:meth:`~reyn.interfaces.web.auth.core.AuthContext.authenticate` seam the WS gate
uses (no new auth is introduced) — an unauthenticated connection is refused
before any session is attached. The per-connection frame source fans the
session's own outbox + the renderer-relevant chat-event subset into one unified
frame stream (the server analogue of :class:`InProcessTransport`), so the wire
carries exactly what the local seam does.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import (
    DisplayFrame,
    EventFrame,
    Frame,
    renderer_chat_events,
)
from reyn.interfaces.web.auth import AuthContext, ConnectionIdentity
from reyn.interfaces.web.deps import get_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agui"])


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
        if self._drain_task is not None:
            self._drain_task.cancel()

    async def _drain_outbox(self) -> None:
        outbox = self._session.outbox
        while True:
            msg = await outbox.get()
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
    """SSE stream of the session's frames as AG-UI events (server→client)."""
    auth = _auth_context(request)
    if auth is None:
        return JSONResponse({"error": "authentication unavailable"}, status_code=401)
    identity = authenticate_request(request, auth)
    if not identity.authenticated:
        return JSONResponse({"error": "authentication required"}, status_code=401)

    registry = get_registry()
    if not registry.exists(agent_name):
        return JSONResponse({"error": f"agent {agent_name!r} not found"}, status_code=404)
    session = await registry.attach(agent_name)

    source = _SessionFrameSource(session)
    source.start()

    def _status_provider():
        # Read-model source: the inline status snapshot for the attached session.
        from reyn.interfaces.inline.app import _snapshot  # noqa: PLC0415

        return _snapshot(registry)

    emitter = AgUiEmitter(source.frames(), _status_provider)

    async def gen():
        try:
            async for chunk in emitter.stream():
                yield chunk
        finally:
            source.close()

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/agui/chat/{agent_name}")
async def agui_submit(request: Request, agent_name: str):
    """Client→server turn submit (P2 scope: basic turn submit only)."""
    auth = _auth_context(request)
    if auth is None:
        return JSONResponse({"error": "authentication unavailable"}, status_code=401)
    identity = authenticate_request(request, auth)
    if not identity.authenticated:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    if not auth.authorize_write(identity):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    registry = get_registry()
    if not registry.exists(agent_name):
        return JSONResponse({"error": f"agent {agent_name!r} not found"}, status_code=404)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    session = await registry.attach(agent_name)
    if payload.get("type") == "user_message":
        text = str(payload.get("text", "")).strip()
        if text:
            await session.submit_user_text(text)
    return JSONResponse({"status": "ok"})


__all__ = ["router", "authenticate_request"]
