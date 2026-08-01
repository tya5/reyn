"""TransportRef — discriminated union for reply-to envelope tagging.

Each variant identifies the logical destination of an outbox message so the
RoutingLayer can fan replies to the correct transport surface:

  TuiRef    → local terminal renderer (``reyn chat``)
  McpRef    → one MCP JSON-RPC request (``reyn mcp serve``)
  A2aRef    → one FastAPI A2A request (``reyn web``)
  AgentRef  → peer-agent inbox (agent-to-agent delegation)
  SystemRef → internal OS messages (task_completed, plan_completed, etc.)

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
    OS (``task_completed``, ``plan_completed``, etc.).  The routing layer
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
# Inbox kind for text arriving over an external transport
# ---------------------------------------------------------------------------

#: The inbox kind every message that arrived over an EXTERNAL transport rides
#: (#3595 step 1b): a chat webhook (``gateway.api.push_to_agent`` — Slack /
#: LINE / any ``reyn.webhooks`` plugin) and an out-of-process request handler
#: (``mcp.server.send_to_agent_impl``, reached by the MCP ``send_to_agent``
#: tool and by the A2A JSON-RPC router).
#:
#: A member of the SAME discriminated union ``Session._run_turn_body`` already
#: dispatches on (``user`` / ``agent_request`` / ``agent_response`` /
#: ``pipeline_result`` / ``agent_step`` / ``hook``). ``"user"`` means "a human
#: typed this at a first-party client", and ``Session._handle_user_message``
#: acts on that claim by handing a ``/``-prefixed line to slash dispatch before
#: any router turn. None of the producers above is that human, so under the old
#: ``kind="user"`` a Slack message reading ``/reset`` executed the command; under
#: this kind the text reaches the turn body directly and no registered slash
#: command is reachable from it at all.
#:
#: **Why ONE kind for two transports.** What the kind has to answer is who
#: authored the text, for the purpose of deciding whether the OS may act on its
#: FORM — and a webhook peer and an MCP/A2A peer answer it identically: a
#: counterparty outside this process, never the operator. Every in-tree consumer
#: of the kind (turn dispatch, ``_stamp_execution_context``, the hook-driven-turn
#: valve, ``queued_user_messages``) branches identically on the two. A consumer
#: that needs the transport ITSELF already has a strictly better source on the
#: envelope — ``sender`` (``"slack:U456"``) or ``reply_to`` (``McpRef`` /
#: ``ExternalRef``) — which names the individual peer, not just its transport.
#: A distinction no consumer branches on, and that a richer field already
#: carries, is a label rather than a union member.
#:
#: Declared HERE rather than at one of the two producers, because neither
#: produces it alone; this module is already the shared vocabulary both import
#: for the same class of thing (``ExternalRef`` / ``McpRef``). Contrast
#: ``hooks.dispatcher.HOOK_INBOX_KIND`` and
#: ``runtime.session_api.AGENT_STEP_INBOX_KIND``, which each have exactly one
#: producer and live in it.
EXTERNAL_MESSAGE_INBOX_KIND = "external_message"


# ---------------------------------------------------------------------------
# Union alias
# ---------------------------------------------------------------------------

TransportRef = TuiRef | McpRef | A2aRef | AgentRef | SystemRef | ExternalRef

__all__ = [
    "EXTERNAL_MESSAGE_INBOX_KIND",
    "TransportRef",
    "TuiRef",
    "McpRef",
    "A2aRef",
    "AgentRef",
    "SystemRef",
    "ExternalRef",
]
