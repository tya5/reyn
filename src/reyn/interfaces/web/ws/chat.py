"""WebSocket route — /ws/chat/{agent_name}.

Bidirectional chat over WebSocket. Each connected client gets its own
asyncio tasks driving the outbox drain. Multiple clients may connect to the
same agent concurrently (all see the same outbox stream).

Protocol (client → server):
    {"type": "user_message", "text": "..."}
        Submit a user turn. If the session has a pending intervention it is
        answered; otherwise a fresh router turn starts.

Protocol (server → client):
    {
        "kind": "<kind>",     # agent | status | error | intervention | trace
        "text": "<text>",
        "meta": {             # optional provenance: run_id, run_id_short, actor,
                              # intervention_id, intervention_kind, choices, ...
            ...
        }
    }

The `kind` taxonomy mirrors src/reyn/interfaces/repl/renderer.py (OS-generic — not
domain-specific per P7). Clients switch on `msg.kind`.

Lifecycle:
  1. connect   → attach agent via AgentRegistry.attach(); start session.run().
  2. on send   → forward text to session.submit_user_text().
  3. on outbox → serialize OutboxMessage to JSON, send to client.
  4. disconnect → detach agent; outbox drain continues in background.

Per P7: no domain-specific strings. OutboxMessage payloads pass through
as opaque JSON (only `kind`, `text`, `meta` — all engine-defined).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from reyn.interfaces.web.auth import AuthContext, ConnectionIdentity
from reyn.interfaces.web.deps import get_registry

router = APIRouter(tags=["websocket"])

logger = logging.getLogger(__name__)

# Outbox message kinds we forward verbatim to the WebSocket client.
# __end__ is a control signal consumed by the registry (= shutdown).
# __attach_request__ used to be dropped here (= the REPL's registry
# forwarder consumed it), but the TUI in ``--connect`` mode owns the
# attached-agent label / conv-clear-on-switch UX (= F13 PR #303's
# ``_on_attach_request`` handler) and needs the sentinel so the
# header label + conv pane stay in sync when a remote ``/attach``
# triggers the server-side swap. Issue #276 Phase B (4/5).
_FORWARDED_KINDS = frozenset({
    "agent", "status", "error", "intervention", "trace",
    "__attach_request__",
})


def _serialize(msg, *, session=None) -> str:
    """Serialize an OutboxMessage to a JSON string for the WS wire.

    Issue #276 Phase B (3/5): when forwarding a ``kind="intervention"``
    frame, augment the meta with ``queued_count`` read from the
    session's ``_interventions`` registry. The TUI in ``--connect``
    mode has no local registry (= the proxy's ``_interventions`` is
    ``None``) so its ``+N more pending`` badge collapsed to 0 even
    when the server held multiple queued items. Inlining the count
    on the way out keeps the TUI's existing ``meta.queued_count``
    fallback path (see ``app_outbox._on_intervention``) populated
    end-to-end without touching the OS-side
    ``InterventionHandler._iv_meta``.
    """
    meta = dict(msg.meta or {})
    if msg.kind == "intervention" and session is not None:
        try:
            registry = getattr(session, "_interventions", None)
            if registry is not None and "queued_count" not in meta:
                meta["queued_count"] = registry.queued_count()
        except Exception:
            pass
    return json.dumps(
        {
            "kind": msg.kind,
            "text": msg.text,
            "meta": meta,
        },
        ensure_ascii=False,
    )


def _decode_inbound_frame(raw: str) -> dict | None:
    """Decode an untrusted client text frame into a JSON object.

    Returns the parsed dict, or ``None`` if *raw* is not valid JSON OR is valid
    JSON that is not an object (e.g. ``123`` / ``[]`` / ``"x"`` / ``null``).
    Centralising the parse here keeps a malformed frame from reaching
    ``payload.get(...)`` downstream (→ ``AttributeError`` on a non-dict) — the
    untrusted A2A/WS inbound boundary.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _unauthorized_frame() -> str:
    """A JSON error frame for a write rejected by delivery-time authorization."""
    return json.dumps({
        "kind": "error",
        "text": "unauthorized: this connection is not authenticated to answer "
        "or run privileged commands.",
        "meta": {"$unauthorized": True},
    })


def _get_auth_context(websocket: WebSocket) -> AuthContext | None:
    """Return the process-wide AuthContext attached to app.state, if any.

    Built once by the server lifespan (``app.state.auth``). A missing context
    means the app was launched outside the gateway startup path; the caller
    fails closed for network peers rather than trusting an unauthenticated one.
    """
    app = getattr(websocket, "app", None)
    state = getattr(app, "state", None)
    return getattr(state, "auth", None)


def _emit_audit(session, event_type: str, identity: ConnectionIdentity, **extra) -> None:
    """Stamp a gateway audit-event carrying the connection identity.

    Answer / attach / detach events record WHO acted (the authenticated
    user-id + connection id + transport tier + OS peer-uid) so a permission
    grant is attributable to the identity that made it. Best-effort — an audit
    emit failure never breaks the connection (P6: the operator's action is the
    primary effect).
    """
    events = getattr(session, "_chat_events", None)
    if events is None:
        return
    try:
        events.emit(event_type, **identity.audit_fields(), **extra)
    except Exception:  # noqa: BLE001 — audit is best-effort
        logger.exception("gateway audit emit failed for %r", event_type)


@router.websocket("/ws/chat/{agent_name}")
async def ws_chat(websocket: WebSocket, agent_name: str) -> None:
    """WebSocket endpoint for a chat session with the named agent."""
    registry = get_registry()

    if not registry.exists(agent_name):
        await websocket.close(code=4004, reason=f"Agent {agent_name!r} not found")
        return

    # ── Authentication gate (server-side; pre-accept) ───────────────────────
    # Every connection carries an identity. A connection that fails
    # authentication (no/invalid token on the loopback/network surface, or a
    # peer-uid mismatch on UDS) is rejected before the chat session is attached
    # — closing the unauthenticated-answer hole. When no AuthContext is present
    # (launched outside the gateway startup path) we fail closed.
    connection_id = secrets.token_hex(8)
    auth = _get_auth_context(websocket)
    if auth is None:
        await websocket.close(code=4401, reason="authentication unavailable")
        return
    identity = auth.authenticate_ws(websocket, connection_id=connection_id)
    if not identity.authenticated:
        await websocket.close(code=4401, reason="authentication required")
        return

    await websocket.accept()

    # Attach the agent: boots session.run() + forwarder if not already running.
    # This pumps the agent's outbox into registry.repl_outbox.
    try:
        session = await registry.attach(agent_name)
    except Exception as exc:
        logger.exception("Failed to attach agent %r", agent_name)
        await websocket.close(code=4000, reason=f"Failed to attach agent: {exc}")
        return

    # Audit: an authenticated client attached to this agent.
    _emit_audit(session, "web_client_attached", identity, agent_name=agent_name)

    # Drain outbox into the WebSocket. We read from the session's own outbox
    # directly (rather than registry.repl_outbox which is REPL-global) so that
    # multiple concurrent WS clients each get their own drain loop and the
    # registry's single-consumer repl_outbox isn't starved.
    #
    # Design note: session.outbox is an asyncio.Queue; multiple coroutines
    # cannot safely call .get() concurrently (only one will receive each item).
    # For the web gateway we create a broadcast multiplexer: a background task
    # reads from session.outbox and fans out to a per-connection queue.
    per_client_q: asyncio.Queue = asyncio.Queue()

    async def _drain_session_outbox() -> None:
        """Fan out session.outbox messages to this client's queue."""
        while True:
            try:
                msg = await asyncio.wait_for(session.outbox.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Keepalive: send a ping-like status so the client knows we're alive.
                try:
                    await websocket.send_text(
                        json.dumps({"kind": "status", "text": "", "meta": {"$keepalive": True}})
                    )
                except Exception:
                    break
                continue
            except Exception:
                break

            if msg.kind == "__end__":
                await per_client_q.put(msg)
                return

            if msg.kind in _FORWARDED_KINDS:
                await per_client_q.put(msg)
            # __attach_request__ and other control signals are dropped here;
            # the registry forwarder handles them.

    drain_task = asyncio.create_task(_drain_session_outbox())

    async def _send_outbox() -> None:
        """Forward per_client_q items to the WebSocket."""
        while True:
            msg = await per_client_q.get()
            if msg.kind == "__end__":
                # Session shut down — close gracefully.
                try:
                    await websocket.close(code=1000, reason="session ended")
                except Exception:
                    pass
                return
            try:
                await websocket.send_text(_serialize(msg, session=session))
            except Exception:
                return

    send_task = asyncio.create_task(_send_outbox())

    try:
        # Receive loop: forward client messages to the session.
        while True:
            raw = await websocket.receive_text()
            payload = _decode_inbound_frame(raw)
            if payload is None:
                # Untrusted inbound: not valid JSON, OR valid JSON that is not an
                # object (``123`` / ``[]`` / ``null``) — the latter would otherwise
                # reach ``payload.get(...)`` → AttributeError. Reject either way.
                await websocket.send_text(json.dumps({
                    "kind": "error",
                    "text": "Client message must be a valid JSON object.",
                    "meta": {},
                }))
                continue

            msg_type = payload.get("type")

            if msg_type == "user_message":
                text = str(payload.get("text", "")).strip()
                if text:
                    await session.submit_user_text(text)
            elif msg_type == "cancel_inflight":
                # Issue #276 Phase B: remote Ctrl+C.
                # #1468: single seam — delegate to session.cancel_inflight()
                # which sets the cooperative turn-cancel flag. This deduplicates
                # the logic that was previously inline here (mirroring
                # app.action_cancel_inflight's local path).
                _cancel_fn = getattr(session, "cancel_inflight", None)
                if callable(_cancel_fn):
                    summary = await _cancel_fn()
                else:
                    summary = "(nothing in-flight on remote to cancel)"
                # Push as a status frame the TUI will surface via its
                # OutboxRouter ``status`` handler.
                await websocket.send_text(json.dumps({
                    "kind": "status",
                    "text": summary,
                    "meta": {"$cancel_ack": True},
                }))
            elif msg_type == "slash_command":
                # Issue #276 Phase B (4/5): forward all slash commands
                # (= ``/agents``, ``/attach``, ``/cost``, ``/budget``,
                # ``/cancel``, etc.) to the server's session. Single
                # routing — the proxy can't reach
                # ``session._registry`` directly in remote mode, so
                # the slash handlers (which read ``_registry`` and
                # other server-side state) run server-side instead.
                # Their replies surface naturally via the existing
                # outbox forwarding (= ``reply`` writes
                # ``kind="system"`` frames, ``reply_error`` writes
                # ``kind="error"``, both already forwarded).
                # Server-side, delivery-time authorization (client-untrusted):
                # slash commands run server-side privileged handlers (attach,
                # budget, cancel) — gate them on the authenticated identity.
                if not auth.authorize_write(identity):
                    await websocket.send_text(_unauthorized_frame())
                    continue
                text = str(payload.get("text", "")).strip()
                if not text or not text.startswith("/"):
                    await websocket.send_text(json.dumps({
                        "kind": "error",
                        "text": "slash_command requires non-empty 'text' starting with '/'.",
                        "meta": {},
                    }))
                    continue
                try:
                    await session._maybe_handle_slash(text)
                except Exception as exc:
                    logger.exception("slash_command failed")
                    await websocket.send_text(json.dumps({
                        "kind": "error",
                        "text": f"slash_command failed: {exc}",
                        "meta": {},
                    }))
            elif msg_type == "answer_intervention":
                # Issue #276 Phase B (2/5): remote intervention answer.
                # The TUI in ``--connect`` mode routes intervention-
                # widget answer_callback → proxy → here. Server runs
                # ``session._maybe_answer_oldest_intervention(text)``
                # which dispatches via the existing
                # ``InterventionHandler.maybe_answer`` (= matches
                # chip-button labels + free-text against the head
                # pending intervention's choices, same as local TUI).
                #
                # KEYSTONE: an intervention answer IS a permission grant. It is
                # authorized SERVER-SIDE, at delivery time, against the server's
                # own record of this connection's authenticated identity (never
                # a client-asserted token) — an unauthenticated connection is
                # rejected here even though it also fails the pre-accept gate
                # (defense in depth; the seam a later seize/takeover phase
                # extends to reject a deposed holder's in-flight answer).
                if not auth.authorize_write(identity):
                    await websocket.send_text(_unauthorized_frame())
                    continue
                text = str(payload.get("text", "")).strip()
                if not text:
                    await websocket.send_text(json.dumps({
                        "kind": "error",
                        "text": "answer_intervention requires non-empty 'text'.",
                        "meta": {},
                    }))
                    continue
                try:
                    answered = await session._maybe_answer_oldest_intervention(
                        text,
                    )
                except Exception as exc:
                    logger.exception("answer_intervention failed")
                    await websocket.send_text(json.dumps({
                        "kind": "error",
                        "text": f"answer_intervention failed: {exc}",
                        "meta": {},
                    }))
                    continue
                if answered:
                    # Audit: attribute the permission grant to the identity.
                    _emit_audit(
                        session, "web_intervention_answered", identity,
                        agent_name=agent_name,
                    )
                else:
                    # No pending intervention matched. Local
                    # ``_maybe_answer_oldest_intervention`` returns
                    # False silently when there's nothing to answer
                    # (= user typed text without an active prompt);
                    # we surface a status frame in the remote case
                    # so the conv pane gets explicit feedback.
                    await websocket.send_text(json.dumps({
                        "kind": "status",
                        "text": "(no pending intervention to answer)",
                        "meta": {"$answer_ack": True, "answered": False},
                    }))
            else:
                # Unknown message type — echo an error but keep the connection.
                await websocket.send_text(json.dumps({
                    "kind": "error",
                    "text": f"Unknown message type {msg_type!r}. "
                    "Expected 'user_message', 'cancel_inflight', "
                    "'answer_intervention', or 'slash_command'.",
                    "meta": {},
                }))

    except WebSocketDisconnect:
        logger.debug("WebSocket disconnected for agent %r", agent_name)
    except Exception as exc:
        logger.exception("WebSocket error for agent %r: %s", agent_name, exc)
    finally:
        drain_task.cancel()
        send_task.cancel()
        # Audit: the authenticated client detached. (Does NOT invalidate any
        # pending intervention answer — the last-surface-gone DENY flow with a
        # grace window is a later phase; disconnect here is audit-only.)
        _emit_audit(session, "web_client_detached", identity, agent_name=agent_name)
        # Detach only if this is still the attached agent — another WS
        # connection may have attached a different agent in the meantime.
        if registry.attached_name == agent_name:
            registry.detach()
        try:
            await asyncio.gather(drain_task, send_task, return_exceptions=True)
        except Exception:
            pass
