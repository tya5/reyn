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

MVP surface (this PR):

  - ``GET /a2a/agents`` — list all Reyn agents (server-level discovery).
  - ``GET /a2a/agents/{name}/.well-known/agent-card.json`` — A2A Agent
    Card per agent (the canonical discovery URL in the A2A spec).
  - ``POST /a2a/agents/{name}`` — JSON-RPC 2.0 endpoint per agent.
    Method: ``message/send`` only (= synchronous, returns final reply
    as an A2A Message). ``message/stream``, task lifecycle (``tasks/get``
    / ``tasks/cancel``), push notifications, and authentication are
    out of scope for v1 and tracked as follow-ups.

P7: this module contains no skill-specific strings. Each Reyn agent's
``role`` text flows through opaquely into the Agent Card description;
the request body's ``message.parts[].text`` is forwarded to
``send_to_agent_impl`` as-is.

Spec reference: https://google.github.io/A2A/
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from reyn.mcp_server import DEFAULT_SEND_TIMEOUT_SECONDS, send_to_agent_impl
from reyn.web.deps import get_registry

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
      * ``capabilities`` — what we DON'T support (= streaming etc.) is
        reported as ``false`` so peers don't try.
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
            "streaming": False,
            "pushNotifications": False,
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
    agent_name: str, request: Request, registry=Depends(get_registry),
) -> dict:
    """JSON-RPC 2.0 endpoint for one Reyn agent.

    Supported methods (MVP):

      - ``message/send`` — submit a single user message, await the
        agent's final reply, return as an A2A Message envelope.

    Unsupported methods return JSON-RPC ``-32601 Method not found`` so a
    peer can capability-fall-back gracefully. Streaming / task lifecycle
    methods land here too (they're real method names in the A2A spec)
    and so receive the same -32601 — peers should consult the Agent
    Card's ``capabilities`` first, but a polite error is the next-best
    thing.

    JSON-RPC framing follows spec strictly: parse / shape errors get
    pre-canonical error codes, business errors (unknown agent, etc.)
    are reported via the ``error`` field rather than HTTP status, since
    HTTP 200 with JSON-RPC error is the accepted convention.
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
        return await _handle_message_send(req_id, body.get("params") or {}, agent_name, registry)

    return _jsonrpc_error(
        req_id,
        _METHOD_NOT_FOUND,
        f"Method not found: {method!r}. Supported: message/send.",
    )


async def _handle_message_send(
    req_id: Any, params: dict, agent_name: str, registry,
) -> dict:
    """Backing for ``message/send``: extract text → send_to_agent_impl
    → wrap reply as A2A Message."""
    if not isinstance(params, dict):
        return _jsonrpc_error(req_id, _INVALID_PARAMS, "params must be an object")

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

    reply_msg = _build_message_response(
        reply_text=result.get("reply", ""),
        partial=bool(result.get("partial", False)),
    )
    return _jsonrpc_result(req_id, reply_msg)


__all__ = ["router"]
