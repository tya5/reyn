"""Which audit-events the A2A + MCP progress fan-outs forward, and how they read.

Two remote protocols expose a best-effort progress stream over a live agent
turn — A2A (``_A2AProgressBridge``: SSE buffer + webhook POST) and MCP
(``_MCPProgressBridge``: ``notifications/progress``). Both subscribe to the
SAME source, the session's chat audit-event log (``Session._audit_events``), and
both must forward the same selection under the same wording so a peer can apply
one parser to either transport. This module is that single declaration; the two
bridges hold no vocabulary of their own.

The selection is a **turn-shaped skeleton**, one line per thing a remote peer
can act on:

  - ``turn_started``  — a turn boundary on this session (``kind`` names the
    inbox trigger: ``"user"`` / ``"agent_response"`` / ``"hook"`` / …).
    Emitted by ``Session.run_one_iteration``.
  - ``llm_called``    — a provider call went out (``model``). Emitted by
    ``reyn.llm.llm``'s cost-event pair against the ambient audit-event log.
  - ``tool_returned`` — a dispatched tool completed (``tool``). Emitted by
    ``reyn.core.dispatch.dispatcher.dispatch_tool``.
  - ``tool_failed``   — a dispatched tool completed by failing (``tool``).
    Same emitter; the two are mutually exclusive per call.

``turn_started`` / ``tool_returned`` / ``tool_failed`` are the same audit-event
kinds the AG-UI transport maps to ``RUN_STARTED`` / ``TOOL_CALL_END`` (see
``docs/reference/runtime/agui-transport.md``) — the remote-client-facing
lifecycle skeleton is deliberately one vocabulary across all three surfaces.

★ Every member MUST have a live emit call-site in ``src/reyn``. The audit-event
``type`` namespace is open (there is no closed vocabulary and no emit-time
schema check — ``event_schema.EVENT_AUDIT_REQUIREMENTS`` declares required
FIELDS for the kinds it covers, not which kinds exist), so a member that no
producer emits degrades the stream silently: the ordinal counter still
increments, and a subscribing peer cannot distinguish a degraded stream from a
quiet run. ``tests/test_progress_lifecycle_fanout_3357.py`` is the liveness
gate that keeps that from happening again (#3357).

The payload of the forwarded notification carries the event kind plus the
one-line message built here — never a tool's arguments or result body.
"""
from __future__ import annotations

# The audit-event kinds both remote progress fan-outs forward. Read the module
# docstring before adding a member: it needs a live emitter AND a message arm.
PROGRESS_LIFECYCLE_EVENTS: "frozenset[str]" = frozenset({
    "turn_started",
    "llm_called",
    "tool_returned",
    "tool_failed",
})


def format_progress_message(event_type: str, data: dict) -> str:
    """Render one forwarded audit-event as a peer-readable progress line.

    ``data`` is the audit-event payload. Every arm reads a single identifying
    field and degrades to ``"?"`` when it is absent, so a payload-shape drift
    costs a legible label rather than the notification. An unrecognised kind
    falls through to its own name (the bridges filter to
    :data:`PROGRESS_LIFECYCLE_EVENTS` before calling, so this is defence in
    depth, not a routing path).
    """
    if event_type == "turn_started":
        return f"turn: {data.get('kind') or '?'}"
    if event_type == "llm_called":
        return f"llm: {data.get('model') or '?'}"
    if event_type == "tool_returned":
        return f"tool: {data.get('tool') or '?'}"
    if event_type == "tool_failed":
        return f"tool: {data.get('tool') or '?'} (failed)"
    return event_type
