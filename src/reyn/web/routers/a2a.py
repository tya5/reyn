"""A2A (Agent2Agent) protocol router — expose Reyn agents as A2A endpoints.

Sister to ``reyn.web.routers.mcp`` — same backing implementation
(``reyn.mcp_server.send_to_agent_impl``), different wire protocol.

  - **MCP** (Model Context Protocol): an *outer LLM client* (Claude
    Desktop, Cursor, …) treats Reyn as a tool provider. Tools are
    ``list_agents`` / ``send_to_agent``.
  - **A2A** (Agent2Agent): peer agents (LangGraph, CrewAI, custom
    agents speaking A2A) treat each Reyn agent as an addressable peer.
    Discovery happens via Agent Cards; conversation happens via
    JSON-RPC 2.0 ``message/send``.

Surface (FP-0001 + MVP):

  - ``GET /a2a/agents`` — list all Reyn agents (server-level discovery).
  - ``GET /a2a/agents/{name}/.well-known/agent-card.json`` — A2A Agent
    Card per agent (the canonical discovery URL in the A2A spec).
  - ``POST /a2a/agents/{name}`` — JSON-RPC 2.0 endpoint per agent.
    Method: ``message/send``. Three operating modes:
    1. **Answer injection** (``params.task_id`` present): deliver an
       answer to a pending ask_user intervention on a running async task.
    2. **Async mode** (``params.async_mode=true`` OR ``params.webhook_url``
       set): spawn a background task, return A2A Task envelope immediately.
    3. **Synchronous** (default): return final reply as A2A Message.
  - ``GET /a2a/tasks/{run_id}`` — poll async task status.
  - ``POST /a2a/tasks/{run_id}/cancel`` — cancel a running task.
  - ``GET /a2a/tasks/{run_id}/events`` — SSE stream of task history.

P7: this module contains no skill-specific strings. Each Reyn agent's
``role`` text flows through opaquely into the Agent Card description;
the request body's ``message.parts[].text`` is forwarded to
``send_to_agent_impl`` as-is.

Spec reference: https://google.github.io/A2A/
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from reyn.mcp_server import DEFAULT_SEND_TIMEOUT_SECONDS, send_to_agent_impl
from reyn.web.deps import get_registry, get_run_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["a2a"])

# ── A2A protocol constants ──────────────────────────────────────────────────

# JSON-RPC 2.0 standard error codes.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

# Reyn's reported A2A protocol version. Bump when we add streaming / task
# lifecycle support so peers can capability-negotiate.
_A2A_PROTOCOL_VERSION = "0.2.0"

# Reyn's own version string (= surfaced in Agent Card so peers can spot
# a stale Reyn instance during interop debugging).
_REYN_A2A_VERSION = "0.1.0"


# ── helpers ─────────────────────────────────────────────────────────────────


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    """Construct a JSON-RPC 2.0 error response envelope."""
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    """Construct a JSON-RPC 2.0 success response envelope."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _build_agent_card(agent_name: str, role: str, base_url: str) -> dict:
    """Construct the A2A Agent Card for a single Reyn agent.

    The card is what peer agents read to decide whether and how to talk
    to this one. We surface:

      * ``name`` — the Reyn agent name (= addressable identity).
      * ``description`` — the agent's ``role`` text from profile.yaml.
      * ``url`` — the JSON-RPC endpoint to POST to.
      * ``capabilities`` — streaming and pushNotifications are now True
        (= FP-0001 adds async task lifecycle + SSE + webhook support).
        ``stateTransitionHistory`` remains False (= no plans to implement).
      * ``skills`` — A2A's ``skill`` is an outward-facing capability,
        not Reyn's internal skill graph. We expose a single coarse-
        grained skill (``chat``) since each Reyn agent's actual
        capabilities are expressed through its underlying Reyn skill
        catalogue, which the OS routes to internally — opaque to the
        A2A peer by design (P7).
    """
    return {
        "name": agent_name,
        "description": role or f"Reyn agent {agent_name!r}",
        "url": base_url,
        "version": _REYN_A2A_VERSION,
        "protocolVersion": _A2A_PROTOCOL_VERSION,
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "chat",
                "name": "Chat with agent",
                "description": (
                    f"Converse with the Reyn agent {agent_name!r}. "
                    "The agent decides internally which Reyn skills "
                    "to invoke; multi-turn history persists across calls."
                ),
                "tags": ["chat", "conversation"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            },
        ],
    }


def _extract_text_from_parts(parts: list) -> str:
    """Pull the concatenated ``text`` out of an A2A message ``parts`` list.

    A2A allows multiple part kinds (``text``, ``file``, ``data``). For
    MVP we forward only text — non-text parts are silently skipped so a
    peer that sends a mixed message still gets a sensible reply. Future
    work: support ``file`` parts via Reyn's workspace upload path.
    """
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if kind == "text":
            text = part.get("text", "")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks)


def _build_message_response(reply_text: str, partial: bool) -> dict:
    """Wrap the agent's reply in an A2A Message envelope.

    A2A's ``message/send`` returns either a Task (= async, polled later)
    or a Message (= synchronous reply). We always return Message because
    Reyn's MCP-symmetric backing is synchronous-with-timeout.

    If the underlying call timed out (``partial=True``) the final part
    carries a metadata flag the peer can surface to its own user; the
    message text itself is whatever ``send_to_agent_impl`` produced
    (typically a "still working" placeholder).
    """
    parts = [{"kind": "text", "text": reply_text}]
    return {
        "kind": "message",
        "role": "agent",
        "parts": parts,
        "messageId": uuid.uuid4().hex,
        "metadata": {"partial": partial} if partial else {},
    }


# ── GET /a2a/agents — server-level discovery ────────────────────────────────


@router.get("/a2a/agents")
async def list_a2a_agents(request: Request, registry=Depends(get_registry)) -> dict:
    """List all A2A-addressable agents on this Reyn instance.

    Not part of the A2A spec proper (which expects each agent to be at
    its own well-known URL), but a convenience for peers that want to
    enumerate what's available before fetching individual cards.
    """
    base = str(request.base_url).rstrip("/")
    out = []
    for name in registry.list_names():
        try:
            profile = registry.load_profile(name)
            role = profile.role or ""
        except Exception as e:  # noqa: BLE001 — defensive
            logger.warning("a2a list: profile load failed for %r: %s", name, e)
            role = ""
        out.append({
            "name": name,
            "role": role,
            "agentCardUrl": f"{base}/a2a/agents/{name}/.well-known/agent-card.json",
            "endpoint": f"{base}/a2a/agents/{name}",
        })
    return {"agents": out, "protocolVersion": _A2A_PROTOCOL_VERSION}


# ── GET /a2a/agents/{name}/.well-known/agent-card.json ──────────────────────


@router.get("/a2a/agents/{agent_name}/.well-known/agent-card.json")
async def get_agent_card(
    agent_name: str, request: Request, registry=Depends(get_registry),
) -> dict:
    """Return the A2A Agent Card for ``agent_name``.

    This URL pattern (``.well-known/agent-card.json``) is the canonical
    A2A discovery endpoint. Peers fetch it before sending any
    ``message/send`` request to learn the agent's capabilities and the
    JSON-RPC URL to POST to.
    """
    if not registry.exists(agent_name):
        raise HTTPException(
            status_code=404,
            detail=f"Reyn agent {agent_name!r} not found on this server.",
        )
    try:
        profile = registry.load_profile(agent_name)
        role = profile.role or ""
    except Exception as e:  # noqa: BLE001 — defensive
        logger.warning("a2a card: profile load failed for %r: %s", agent_name, e)
        role = ""

    base = str(request.base_url).rstrip("/")
    endpoint = f"{base}/a2a/agents/{agent_name}"
    return _build_agent_card(agent_name, role, endpoint)


# ── POST /a2a/agents/{name} — JSON-RPC 2.0 endpoint ─────────────────────────


@router.post("/a2a/agents/{agent_name}")
async def a2a_jsonrpc(
    agent_name: str,
    request: Request,
    registry=Depends(get_registry),
    run_registry=Depends(get_run_registry),
) -> dict:
    """JSON-RPC 2.0 endpoint for one Reyn agent.

    Supported method: ``message/send``. Three modes:

    1. **Answer injection** (``params.task_id`` present): deliver an
       answer to a pending ask_user intervention on a running async task.
       Returns ``{"task_id": ..., "answered": True/False}``.

    2. **Async mode** (``params.async_mode=true`` OR ``params.webhook_url``
       set): spawn a background task and return an A2A Task envelope
       immediately. Poll ``GET /a2a/tasks/{run_id}`` for status.

    3. **Synchronous** (default — existing behaviour): submit a user
       message, await the agent's final reply, return as an A2A Message
       envelope.

    Unsupported methods return JSON-RPC ``-32601 Method not found``.
    """
    # Parse body. JSON parse errors are -32700; anything else short of a
    # well-formed envelope is -32600.
    try:
        body = await request.json()
    except Exception:
        return _jsonrpc_error(None, _PARSE_ERROR, "Parse error: invalid JSON body")

    if not isinstance(body, dict):
        return _jsonrpc_error(None, _INVALID_REQUEST, "Invalid Request: body must be an object")

    req_id = body.get("id")
    if body.get("jsonrpc") != "2.0":
        return _jsonrpc_error(req_id, _INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'")

    method = body.get("method")
    if not isinstance(method, str):
        return _jsonrpc_error(req_id, _INVALID_REQUEST, "Invalid Request: method must be a string")

    # Route to handlers.
    if method == "message/send":
        return await _handle_message_send(
            req_id, body.get("params") or {}, agent_name, registry, run_registry,
        )

    return _jsonrpc_error(
        req_id,
        _METHOD_NOT_FOUND,
        f"Method not found: {method!r}. Supported: message/send.",
    )


async def _handle_message_send(
    req_id: Any,
    params: dict,
    agent_name: str,
    registry,
    run_registry,
) -> dict:
    """Backing for ``message/send``.

    Three modes (checked in priority order):

    1. **Answer injection** — ``params.task_id`` non-empty: resolve a
       pending ask_user on an existing async run.
    2. **Async mode** — ``params.async_mode is True`` OR
       ``params.webhook_url`` set: spawn background task, return Task.
    3. **Synchronous** (default): blocking send, return Message.
    """
    if not isinstance(params, dict):
        return _jsonrpc_error(req_id, _INVALID_PARAMS, "params must be an object")

    # ── Mode 1: answer injection ──────────────────────────────────────────
    task_id = params.get("task_id")
    if task_id and isinstance(task_id, str):
        return await _handle_answer_injection(req_id, task_id, params, run_registry)

    # ── Shared: extract text from message parts ───────────────────────────
    message = params.get("message")
    if not isinstance(message, dict):
        return _jsonrpc_error(req_id, _INVALID_PARAMS, "params.message is required")

    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        return _jsonrpc_error(
            req_id, _INVALID_PARAMS, "params.message.parts must be a non-empty array",
        )

    text = _extract_text_from_parts(parts)
    if not text.strip():
        return _jsonrpc_error(
            req_id,
            _INVALID_PARAMS,
            "params.message.parts must contain at least one non-empty text part. "
            "(Non-text parts are not yet supported by this Reyn endpoint.)",
        )

    # ── Mode 2: async mode ────────────────────────────────────────────────
    async_mode = params.get("async_mode")
    webhook_url = params.get("webhook_url") or None
    if async_mode is True or webhook_url:
        return await _handle_async_mode(
            req_id, text, agent_name, registry, run_registry, webhook_url,
        )

    # ── Mode 3: synchronous (default) ────────────────────────────────────
    try:
        result = await send_to_agent_impl(
            registry,
            agent_name=agent_name,
            message=text,
            timeout=DEFAULT_SEND_TIMEOUT_SECONDS,
        )
    except ValueError as e:
        # Unknown agent: surface as JSON-RPC error rather than HTTP 404
        # so peers parsing the JSON-RPC envelope get a uniform shape.
        return _jsonrpc_error(req_id, _INVALID_PARAMS, f"Unknown agent: {e}")
    except Exception as e:  # noqa: BLE001 — defensive
        logger.exception("a2a message/send: backing impl raised")
        return _jsonrpc_error(req_id, _INTERNAL_ERROR, f"Internal error: {e}")

    # B42-NF-W6-2: A2A spec-compliant auto-escalation. When sync mode
    # times out with a skill still running, return a Task envelope (=
    # kind="task" with id) instead of a partial Message — the standard
    # A2A discriminator that tells the caller to poll
    # ``GET /a2a/tasks/{id}``. Without this, a long-running skill spawned
    # via sync ``message/send`` loses its completion narration: the
    # spawn-ack returns but no subsequent request arrives to drive the
    # skill_completion_injected inbox drain, so the run becomes a silent
    # tombstone (B42 W6-S6 reproduction).
    running_ids = result.get("running_skill_run_ids") or []
    if result.get("partial") and running_ids:
        return await _escalate_to_task(
            req_id, agent_name, running_ids, registry, run_registry,
        )

    reply_msg = _build_message_response(
        reply_text=result.get("reply", ""),
        partial=bool(result.get("partial", False)),
    )
    return _jsonrpc_result(req_id, reply_msg)


async def _escalate_to_task(
    req_id: Any,
    agent_name: str,
    running_skill_run_ids: list[str],
    registry,
    run_registry,
) -> dict:
    """Auto-escalate a sync ``message/send`` that timed out with a still-
    running skill into an A2A Task envelope.

    Per A2A v0.2.0 spec, ``message/send`` may return either a ``Message``
    (= synchronous reply) or a ``Task`` (= async, polled later) result.
    The server chooses based on operation duration. This helper performs
    the Task-path leg: register a ``RunEntry`` (so ``GET /a2a/tasks/{id}``
    can serve status), spawn a monitor task that pumps the session until
    the skill's completion narration lands, and return the Task envelope.

    The caller (= A2A peer) inspects ``result.kind`` to decide whether to
    consume parts directly or follow up with ``tasks/get`` polling. This
    is the spec discriminator: peers MUST handle both shapes.
    """
    from reyn.chat.session import _new_chain_id  # noqa: PLC0415

    # The skill's run_id is what the monitor uses internally to detect
    # completion (= it polls session.running_skills[skill_run_id]). When
    # multiple skills are in flight (rare; the LLM normally spawns at most
    # one per turn), the monitor watches the first — the others remain
    # trackable via session state but only the headline gets the task
    # envelope.
    skill_run_id = running_skill_run_ids[0]
    chain_id = _new_chain_id()

    # Allocate a fresh RunRegistry entry. The entry.run_id is a NEW uuid
    # (distinct from the skill's run_id) that becomes the caller-facing
    # task id polled via ``GET /a2a/tasks/{entry.run_id}``. This indirection
    # is intentional: the A2A task lifecycle (= caller-facing) is owned by
    # the RunRegistry; the skill's run_id (= OS-internal) stays inside the
    # monitor's await-loop and never leaks to the caller.
    entry = run_registry.create(
        agent_name=agent_name,
        chain_id=chain_id,
    )
    monitor_task_id = entry.run_id

    async def _monitor() -> None:
        """Pump the session until the running skill completes, then mark
        the run_registry entry with the harvested narration.

        Uses MessageBus.request with a long timeout so the completion
        narration is consumed and emitted as a router reply. The reply
        text becomes the Task ``result`` field, available on subsequent
        ``GET /a2a/tasks/{id}`` calls.

        On deadline expiry (= the skill is still running after the
        monitor's wait window), the entry is marked ``status="timeout"``
        rather than ``"completed"`` so the caller doesn't conflate "we
        gave up waiting" with "the skill produced a real result". Plain
        success path remains ``status="completed"``.
        """
        try:
            # Wait until the running skill's asyncio task is done, plus a
            # final pump pass to consume the skill_completion_injected
            # inbox message and surface the narration.
            session = await _get_session_for_monitor(registry, agent_name)
            completed = await _await_skill_completion(
                session, skill_run_id, deadline_s=600.0,
            )
            if not completed:
                run_registry.update(
                    monitor_task_id,
                    status="timeout",
                    error=(
                        f"skill {skill_run_id} did not complete within "
                        f"the monitor deadline; the underlying skill "
                        f"task continues in the session"
                    ),
                )
                return
            narration = _harvest_completion_narration(session, skill_run_id)
            run_registry.update(
                monitor_task_id,
                status="completed",
                result=narration or "(no narration captured)",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("a2a auto-escalation monitor raised")
            run_registry.update(monitor_task_id, status="failed", error=str(exc))

    monitor = asyncio.create_task(_monitor())
    run_registry.attach_task(monitor_task_id, monitor)

    return _jsonrpc_result(
        req_id,
        {
            "kind": "task",
            "id": monitor_task_id,
            "status": "running",
            "agent_name": agent_name,
        },
    )


async def _get_session_for_monitor(registry, agent_name: str):
    """Resolve the ChatSession instance for the monitor task.

    Mirrors ``mcp_server._get_session`` but kept local so the import
    surface of this router stays small.
    """
    return registry.get_or_load(agent_name)


async def _await_skill_completion(
    session, skill_run_id: str, *, deadline_s: float = 600.0,
) -> bool:
    """Poll ``session.running_skills`` until ``skill_run_id`` is no longer
    active, OR the deadline fires.

    The skill's asyncio.Task being done() is the OS-level signal that the
    skill reached a terminal state (success / error / interrupted). After
    that, a final session iteration drains the queued ``skill_completed``
    inbox entry so the narration LLM turn fires.

    Returns ``True`` when the skill task reached terminal state within
    the deadline (= caller can safely harvest narration); ``False`` when
    the deadline expired with the task still running (= caller should
    mark the run_registry entry as ``status="timeout"`` rather than
    ``"completed"`` to avoid conflating "we gave up waiting" with a real
    completion).
    """
    deadline = asyncio.get_event_loop().time() + deadline_s
    while True:
        running = getattr(session, "running_skills", {}) or {}
        task = running.get(skill_run_id)
        if task is None or task.done():
            break
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.5)
    # Final inbox drain: pump iterations until quiescent so the
    # skill_completion_injected inbox entry is consumed and the
    # narration turn lands in history.
    for _ in range(20):  # bounded; each iteration consumes one inbox msg
        if session.inbox.empty():
            break
        await session.run_one_iteration()
    return True


def _harvest_completion_narration(session, skill_run_id: str) -> str:
    """Pull the most recent narration text from session history.

    Heuristic: the narration is the latest ``role="agent"`` history
    entry whose preceding entry is a ``meta.source="skill_completion"``
    user-role injection for this ``skill_run_id``. Falls back to "the
    last non-spawn-ack agent message" if the injection-pair cannot be
    located (= defensive against future history-shape tweaks).
    """
    history = list(getattr(session, "history", []) or [])
    # Walk backwards looking for a skill_completion injection for this run_id
    for i in range(len(history) - 1, 0, -1):
        msg = history[i]
        prev = history[i - 1]
        meta = getattr(msg, "meta", None) or {}
        prev_meta = getattr(prev, "meta", None) or {}
        if (
            getattr(msg, "role", None) == "agent"
            and meta.get("source") != "spawn_ack"
            and prev_meta.get("source") == "skill_completion"
            and prev_meta.get("run_id") == skill_run_id
        ):
            return getattr(msg, "text", "") or ""
    # Fallback: latest non-spawn-ack agent message.
    for msg in reversed(history):
        meta = getattr(msg, "meta", None) or {}
        if getattr(msg, "role", None) == "agent" and meta.get("source") != "spawn_ack":
            return getattr(msg, "text", "") or ""
    return ""


async def _handle_answer_injection(
    req_id: Any,
    task_id: str,
    params: dict,
    run_registry,
) -> dict:
    """Deliver an answer to a pending ask_user intervention on an async task.

    Extracts text from ``params.message.parts`` (same as normal send),
    then calls ``run_registry.answer_intervention``.
    """
    from reyn.user_intervention import InterventionAnswer  # noqa: PLC0415

    # Extract answer text from message parts (if provided).
    message = params.get("message")
    answer_text = ""
    if isinstance(message, dict):
        parts = message.get("parts") or []
        if isinstance(parts, list):
            answer_text = _extract_text_from_parts(parts)

    answer = InterventionAnswer(text=answer_text)
    delivered = run_registry.answer_intervention(task_id, answer)

    if delivered:
        result = {"task_id": task_id, "answered": True}
    else:
        entry = run_registry.get(task_id)
        if entry is None:
            reason = "not found"
        else:
            reason = "already answered or no pending intervention"
        result = {"task_id": task_id, "answered": False, "reason": reason}

    return _jsonrpc_result(req_id, result)


async def _handle_async_mode(
    req_id: Any,
    text: str,
    agent_name: str,
    registry,
    run_registry,
    webhook_url: str | None,
) -> dict:
    """Spawn a background asyncio task and return an A2A Task envelope."""
    from reyn.chat.session import _new_chain_id  # noqa: PLC0415
    from reyn.web.a2a_intervention import A2AInterventionBus  # noqa: PLC0415

    chain_id = _new_chain_id()
    entry = run_registry.create(
        agent_name=agent_name,
        chain_id=chain_id,
        webhook_url=webhook_url,
    )
    run_id = entry.run_id

    bus = A2AInterventionBus(run_id, run_registry)

    async def _run() -> None:
        try:
            result = await send_to_agent_impl(
                registry,
                agent_name=agent_name,
                message=text,
                timeout=DEFAULT_SEND_TIMEOUT_SECONDS,
                intervention_override=bus,
            )
            run_registry.update(
                run_id,
                status="completed",
                result=result.get("reply", ""),
            )
            if webhook_url:
                from reyn.web.notifications import post_webhook  # noqa: PLC0415
                await post_webhook(
                    webhook_url,
                    {
                        "run_id": run_id,
                        "status": "completed",
                        "result": result.get("reply", ""),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("a2a async task %r raised", run_id)
            run_registry.update(run_id, status="failed", error=str(exc))
            if webhook_url:
                from reyn.web.notifications import post_webhook  # noqa: PLC0415
                await post_webhook(
                    webhook_url,
                    {"run_id": run_id, "status": "failed", "error": str(exc)},
                )

    task = asyncio.create_task(_run())
    run_registry.attach_task(run_id, task)

    return _jsonrpc_result(
        req_id,
        {
            "kind": "task",
            "id": run_id,
            "status": "running",
            "agent_name": agent_name,
        },
    )


# ── GET /a2a/tasks/{run_id} — poll task status ───────────────────────────────


@router.get("/a2a/tasks/{run_id}")
async def get_task(
    run_id: str,
    run_registry=Depends(get_run_registry),
) -> dict:
    """Poll a task's status. Returns RunEntry.to_public_dict()."""
    entry = run_registry.get(run_id)
    if entry is None:
        raise HTTPException(404, f"Task {run_id!r} not found")
    return entry.to_public_dict()


# ── POST /a2a/tasks/{run_id}/cancel — cancel a running task ─────────────────


@router.post("/a2a/tasks/{run_id}/cancel")
async def cancel_task(
    run_id: str,
    run_registry=Depends(get_run_registry),
) -> dict:
    """Cancel a running task. Idempotent for already-terminal tasks."""
    if not run_registry.cancel(run_id):
        raise HTTPException(404, f"Task {run_id!r} not found")
    entry = run_registry.get(run_id)
    return entry.to_public_dict() if entry else {"run_id": run_id, "status": "cancelled"}


# ── GET /a2a/tasks/{run_id}/events — SSE stream ──────────────────────────────


@router.get("/a2a/tasks/{run_id}/events")
async def stream_task_events(
    run_id: str,
    run_registry=Depends(get_run_registry),
):
    """SSE stream of the task's history_events.

    Replays already-buffered events on connect; then polls for new
    events every 0.5s until the task reaches a terminal status
    (completed / failed / cancelled). Returns FastAPI StreamingResponse
    with media_type='text/event-stream'.
    """
    import json  # noqa: PLC0415

    from fastapi.responses import StreamingResponse  # noqa: PLC0415

    async def gen():
        if run_registry.get(run_id) is None:
            yield 'event: error\ndata: {"error": "not_found"}\n\n'
            return
        seen = 0
        terminal = {"completed", "failed", "cancelled"}
        while True:
            entry = run_registry.get(run_id)
            if entry is None:
                yield 'event: error\ndata: {"error": "gone"}\n\n'
                return
            for ev in entry.history_events[seen:]:
                yield f"data: {json.dumps(ev)}\n\n"
            seen = len(entry.history_events)
            if entry.status in terminal:
                yield f"event: end\ndata: {json.dumps(entry.to_public_dict())}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")


__all__ = ["router"]
