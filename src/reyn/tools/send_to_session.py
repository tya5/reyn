"""send_to_session ToolDefinition — proposal 0067 P5 (#3978).

Router-only (gates.router=allow).

Fire-and-forget delivery semantics
-----------------------------------
send_to_session is NOT a request/response tool, and unlike
delegate_to_agent it never registers a pending_chain: there is no reply to
collect. RouterLoop calls self.host.send_to_session(...) and returns the
delivery status (delivered/not) immediately — the target session's own
future turn (if any) is entirely its own affair. `wake` selects only
whether the target's run-loop is booted now (wake=True) or the message
waits queued as context for whenever the target next runs a turn on its
own (wake=False) — see TurnOrigin.PEER_SESSION's docstring for the member
this rides on, and Session._deliver_cross_session_message for the delivery
substrate (also used, generalized, by #2072's cross-session hook push).

dispatch_kind="sync": every call returns immediately with a status dict,
never ends the turn (unlike delegate_to_agent's "async" posture).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import delegation as _delegation_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_SEND_TO_SESSION_DESCRIPTION = _delegation_descriptions.send_to_session.text

_SEND_TO_SESSION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["send_to_session"]["agent"].text,
        },
        "session": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["send_to_session"]["session"].text,
        },
        "text": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["send_to_session"]["text"].text,
        },
        "wake": {
            "type": "boolean",
            "description": _delegation_descriptions.PARAMS["send_to_session"]["wake"].text,
            "default": False,
        },
    },
    "required": ["agent", "session", "text"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Delegate to RouterCallerState.send_to_session_fn.

    Raises RuntimeError when router_state or send_to_session_fn is missing
    (= mis-wiring; matches delegate_to_agent's handler convention) — a host
    that genuinely doesn't support multi-session delivery leaves
    send_to_session_fn None, and the tool should not even be catalog-visible
    on such a host (schema_enricher / catalog filtering is a follow-on
    concern shared with the other duck-typed dispatch tools, not specific
    to this one).
    """
    rs = ctx.router_state
    if rs is None or rs.send_to_session_fn is None:
        raise RuntimeError(
            "send_to_session handler requires ctx.router_state.send_to_session_fn "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    return await rs.send_to_session_fn(
        agent=args["agent"], session=args["session"],
        text=args["text"], wake=bool(args.get("wake", False)),
    )


from reyn.core.offload.canonical import send_to_session_to_canonical  # noqa: E402

SEND_TO_SESSION = ToolDefinition(
    canonical=send_to_session_to_canonical,
    name="send_to_session",
    router_dispatched=True,
    description=_SEND_TO_SESSION_DESCRIPTION,
    parameters=_SEND_TO_SESSION_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="sync",  # delivery-only: returns immediately, never ends the turn
)
