"""session_spawn ToolDefinition — #2103 S1bc (LLM session-spawn primitive).

Router-only (gates.router=allow, gates.phase=deny). Async-dispatch posture: the LLM
spawns a FRESH-context session under its own agent to run a task in isolation; the
handler calls ctx.router_state.spawn_session_fn(...) and returns a spawn-ack. The
spawned session RUNS the task (its run-loop is started); the result stays in the
spawned session — routing it back to the spawner is the S1bc-exec follow-on (FP-0043
Stage-4 non-main routing).

Scope-time mode (the owner's explicit spawn-time choice): ``mode`` is ephemeral |
persistent. Both are rewind-safe (a session spawned after a rewind cut is dropped). The
ephemeral auto-vanish (after the task) is the immediate-next sub-slice; the mode is
recorded now (on the ``session_spawned`` WAL event).

``narrowing`` (optional) is a per-session capability narrowing (restrict-only, the
#2103 S1a 4th COMBINE layer) — a capability_profile subset the spawner imposes on the
sub-session; it is workspace-backed (config.yaml) + composed at construction.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import delegation as _delegation_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Reviewable in src/reyn/tools/descriptions/delegation.py (Phase 2 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_SESSION_SPAWN_DESCRIPTION = _delegation_descriptions.session_spawn.text

_SESSION_SPAWN_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "request": {
            "type": "string",
            "description": "The task for the fresh-context session to run.",
        },
        "mode": {
            "type": "string",
            "enum": ["ephemeral", "persistent"],
            "default": "persistent",
            "description": (
                "ephemeral = the session auto-vanishes after its task; "
                "persistent = it stays. Chosen at spawn time."
            ),
        },
        "narrowing": {
            "type": "object",
            "description": (
                "Optional per-session capability narrowing (restrict-only, cannot "
                "widen your envelope): a capability_profile subset, e.g. "
                "{\"tool_deny\": [\"sandboxed_exec\"]}."
            ),
        },
    },
    "required": ["request"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch to RouterCallerState.spawn_session_fn (#2103 S1bc).

    Async-dispatch posture: returns a spawn-ack immediately; the spawned session runs
    the task in isolation. Raises RuntimeError when the host doesn't support
    session-spawn (= mis-wiring / a non-multi-session host)."""
    rs = ctx.router_state
    if rs is None or rs.spawn_session_fn is None:
        raise RuntimeError(
            "session_spawn requires ctx.router_state.spawn_session_fn — unavailable "
            "(host does not support session-spawn / mis-wired dispatcher)."
        )
    mode = args.get("mode", "persistent")
    if mode not in ("ephemeral", "persistent"):
        return {
            "status": "error",
            "kind": "invalid_mode",
            "error": f"mode must be 'ephemeral' or 'persistent', got {mode!r}.",
        }
    return await rs.spawn_session_fn(
        request=args["request"], mode=mode, narrowing=args.get("narrowing"),
    )


from reyn.core.offload.canonical import session_spawn_to_canonical  # noqa: E402

SESSION_SPAWN = ToolDefinition(
    canonical=session_spawn_to_canonical,
    name="session_spawn",
    router_dispatched=True,
    description=_SESSION_SPAWN_DESCRIPTION,
    parameters=_SESSION_SPAWN_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="async",  # the spawned session runs the task; result not returned inline
)
