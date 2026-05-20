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
        "kind": "<kind>",     # agent | status | error | intervention | trace | skill_done
        "text": "<text>",
        "meta": {             # optional provenance: run_id, run_id_short, skill_name,
                              # intervention_id, intervention_kind, choices, ...
            ...
        }
    }

The `kind` taxonomy mirrors src/reyn/chat/renderer.py (OS-generic — not
skill-specific per P7). Clients switch on `msg.kind`.

Lifecycle:
  1. connect   → attach agent via AgentRegistry.attach(); start session.run().
  2. on send   → forward text to session.submit_user_text().
  3. on outbox → serialize OutboxMessage to JSON, send to client.
  4. disconnect → detach agent; outbox drain continues in background.

Per P7: no skill-specific strings. OutboxMessage payloads pass through
as opaque JSON (only `kind`, `text`, `meta` — all engine-defined).
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from reyn.web.deps import get_registry

router = APIRouter(tags=["websocket"])

logger = logging.getLogger(__name__)

# Outbox message kinds we forward verbatim to the WebSocket client.
# __end__ and __attach_request__ are control signals consumed by the registry.
_FORWARDED_KINDS = frozenset({
    "agent", "status", "error", "intervention", "trace", "skill_done",
})


def _serialize(msg) -> str:
    """Serialize an OutboxMessage to a JSON string for the WS wire."""
    return json.dumps(
        {
            "kind": msg.kind,
            "text": msg.text,
            "meta": msg.meta or {},
        },
        ensure_ascii=False,
    )


@router.websocket("/ws/chat/{agent_name}")
async def ws_chat(websocket: WebSocket, agent_name: str) -> None:
    """WebSocket endpoint for a chat session with the named agent."""
    registry = get_registry()

    if not registry.exists(agent_name):
        await websocket.close(code=4004, reason=f"Agent {agent_name!r} not found")
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
                await websocket.send_text(_serialize(msg))
            except Exception:
                return

    send_task = asyncio.create_task(_send_outbox())

    try:
        # Receive loop: forward client messages to the session.
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "kind": "error",
                    "text": "Invalid JSON in client message.",
                    "meta": {},
                }))
                continue

            msg_type = payload.get("type")

            if msg_type == "user_message":
                text = str(payload.get("text", "")).strip()
                if text:
                    await session.submit_user_text(text)
            elif msg_type == "cancel_inflight":
                # Issue #276 Phase B: remote Ctrl+C. Iterate the
                # session's running_skills / running_plans and call
                # ``.cancel()`` on each — mirrors what
                # ``app.action_cancel_inflight`` does for the local
                # session. The TUI thin client side delegates here
                # because the proxy's local ``running_skills`` /
                # ``running_plans`` dicts are empty (server-side state
                # isn't replicated). Reports the result back as a
                # ``status`` outbox so the conv pane gets the same
                # "✗ cancelled N skill + M plan" summary local mode
                # shows.
                cancelled_skills = 0
                for task in list(getattr(session, "running_skills", {}).values()):
                    if not task.done():
                        task.cancel()
                        cancelled_skills += 1
                cancelled_plans = 0
                for task in list(getattr(session, "running_plans", {}).values()):
                    if not task.done():
                        task.cancel()
                        cancelled_plans += 1
                if cancelled_skills == 0 and cancelled_plans == 0:
                    summary = "(nothing in-flight on remote to cancel)"
                else:
                    parts: list[str] = []
                    if cancelled_skills:
                        parts.append(
                            f"{cancelled_skills} skill"
                            f"{'s' if cancelled_skills != 1 else ''}"
                        )
                    if cancelled_plans:
                        parts.append(
                            f"{cancelled_plans} plan"
                            f"{'s' if cancelled_plans != 1 else ''}"
                        )
                    summary = f"✗ cancelled {' + '.join(parts)}"
                # Push as a status frame the TUI will surface via its
                # OutboxRouter ``status`` handler.
                await websocket.send_text(json.dumps({
                    "kind": "status",
                    "text": summary,
                    "meta": {
                        "$cancel_ack": True,
                        "cancelled_skills": cancelled_skills,
                        "cancelled_plans": cancelled_plans,
                    },
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
                if not answered:
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
                    "or 'answer_intervention'.",
                    "meta": {},
                }))

    except WebSocketDisconnect:
        logger.debug("WebSocket disconnected for agent %r", agent_name)
    except Exception as exc:
        logger.exception("WebSocket error for agent %r: %s", agent_name, exc)
    finally:
        drain_task.cancel()
        send_task.cancel()
        # Detach only if this is still the attached agent — another WS
        # connection may have attached a different agent in the meantime.
        if registry.attached_name == agent_name:
            registry.detach()
        try:
            await asyncio.gather(drain_task, send_task, return_exceptions=True)
        except Exception:
            pass
