"""TransportRef — discriminated union for reply-to envelope tagging.

Each variant identifies the logical destination of an outbox message so the
RoutingLayer can fan replies to the correct transport surface:

  TuiRef    → local terminal renderer (``reyn chat``)
  McpRef    → one MCP JSON-RPC request (``reyn mcp serve``)
  A2aRef    → one FastAPI A2A request (``reyn web``)
  AgentRef  → peer-agent inbox (agent-to-agent delegation)
  SystemRef → internal OS messages (skill_completed, plan_completed, etc.)

FP-0013: TransportRef is additive. ``reply_to=None`` on Inbox/OutboxMessage
is interpreted as the default surface (TuiRef or SystemRef, depending on
context) for backward compatibility during migration.

ADR-B note: refs are purely runtime objects in this implementation — they do
NOT survive crash recovery.  ``AgentRef`` may need persistence in a later
wave; ``McpRef`` / ``A2aRef`` die with the process by design.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TuiRef:
    """Local terminal renderer — the ``reyn chat`` / TUI surface."""


@dataclass(frozen=True)
class McpRef:
    """One MCP JSON-RPC request.

    ``request_id`` matches the JSON-RPC ``id`` field so the routing layer
    can correlate the reply with the pending future.
    """
    request_id: str


@dataclass(frozen=True)
class A2aRef:
    """One FastAPI A2A request.

    ``request_id`` is a synthetic UUID generated per call by
    ``_handle_message_send`` so the routing layer can correlate.
    """
    request_id: str


@dataclass(frozen=True)
class AgentRef:
    """Peer-agent inbox — target of a cross-agent delegation.

    ``agent_name`` is the target agent; ``chain_id`` identifies the
    delegation chain so the receiving session can route the response back.
    """
    agent_name: str
    chain_id: str


@dataclass(frozen=True)
class SystemRef:
    """Internal OS message with no external sender.

    Used for inbox kinds that originate from background tasks within the
    OS (``skill_completed``, ``plan_completed``, etc.).  The routing layer
    ignores these — they are consumed by ``run()`` internally and produce
    outbox messages with whatever reply_to the turn inherited.
    """


@dataclass(frozen=True)
class ExternalRef:
    """External chat transport routed via an MCP tool call (FP-0041 #489 PR-C).

    Used when an inbound webhook handler (= Slack/LINE/Discord/etc.,
    landing in PR-D / PR-E) encodes the reply destination on the
    inbox envelope. The outbox routing layer (= future PR-D wiring)
    matches ``transport`` to a configured MCP tool (= e.g.
    ``slack__chat_postMessage``) and dispatches the reply text +
    destination metadata to that tool.

    Fields:
      transport: short transport name (= "slack" / "line" / "discord").
        Maps to an MCP tool via ``ExternalTransportRouting`` config.
      destination: opaque transport-specific routing dict (= e.g.
        {"channel": "C123", "thread_ts": "1234.5678"} for Slack,
        {"user_id": "U456"} for LINE). Forwarded to the MCP tool as
        args via the configured args template.
    """
    transport: str
    destination: dict


# ---------------------------------------------------------------------------
# Union alias
# ---------------------------------------------------------------------------

TransportRef = TuiRef | McpRef | A2aRef | AgentRef | SystemRef | ExternalRef

__all__ = [
    "TransportRef",
    "TuiRef",
    "McpRef",
    "A2aRef",
    "AgentRef",
    "SystemRef",
    "ExternalRef",
]
