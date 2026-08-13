"""spawn_session ToolDefinition — #2103 S1bc (LLM session-spawn primitive).
Renamed from session_spawn (#4004) — the module and its internal
identifiers keep the old spelling; only the registered ``name=`` (the
LLM-visible string) and everything keyed off it changed.

Router-only (gates.router=allow). Async-dispatch posture: the LLM
spawns a FRESH-context session under its own agent (or, with the optional
``agent`` argument, #4556: under any agent in its own spawn subtree — the
same ``is_spawn_descendant`` predicate ``create_topology`` uses) to run a task
in isolation; the handler calls ctx.router_state.spawn_session_fn(...) and
returns a spawn-ack. The spawned session RUNS the task (its run-loop is
started); the result stays in the spawned session — routing it back to the
spawner is the S1bc-exec follow-on (FP-0043 Stage-4 non-main routing; the
#2130 run_prompt/agent_response mechanism already covers explicitly pulling
a spawned-under-another-agent session's result back).

Scope-time mode (the owner's explicit spawn-time choice): ``mode`` is ephemeral |
persistent. Both are rewind-safe (a session spawned after a rewind cut is dropped). The
ephemeral auto-vanish (after the task) is the immediate-next sub-slice; the mode is
recorded now (on the ``session_spawned`` WAL event).

``narrowing`` (optional) is a per-session capability narrowing (restrict-only, the
#2103 S1a 4th COMBINE layer) — a capability_profile subset the spawner imposes on the
sub-session; it is workspace-backed (config.yaml) + composed at construction.

Restrict-only is enforced against the SPAWNER, not just asserted (#3556): this argument
is LLM-authored, so ``RouterHostAdapter.spawn_session`` composes the spawning session's
own sid-keyed narrowing into it (``compose_narrowing_mappings`` — denies union, allows
intersect, an absent allow key is ⊤) before the child's ``config.yaml`` is written. Until
#3556 the argument WAS the whole value, and a narrowed session could hand a sibling a
wider envelope than its own. Measured by
``tests/runtime/test_3556_session_spawn_narrowing_inheritance.py``; the layers this does NOT
carry (the #2285 ``/visibility`` toggle, the #1827-S4b ephemeral untrusted-context
narrowing) are the same ones the sibling spawn sites leave behind.

``base_dir`` (optional, #4200 2/2) is the spawn-time SPECIFICATION mechanism for the
session-layer ``base_dir`` override #4200 1/2 taught ``Session._workspace_base_dir`` to
read: LLM-authored, so restrict-only in the SAME sense as ``narrowing`` — resolved and
validated against the SPAWNER's own EFFECTIVE ``base_dir`` (not the spawner's Agent
default; ``Session._workspace_base_dir``'s own resolved value, so a chain of
restrict-only spawns cannot compound-widen) BEFORE the child's ``config.yaml`` is
written. A requested path outside that subtree is REJECTED (never silently clamped into
it, per the #4179 lesson on LLM-writable bounded surfaces) with a message naming the
actual boundary. Omitted → the child inherits the spawner's own ``base_dir`` unchanged
(#4200's own required default).
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
            "description": _delegation_descriptions.PARAMS["spawn_session"]["request"].text,
        },
        "mode": {
            "type": "string",
            "enum": ["ephemeral", "persistent"],
            "default": "persistent",
            "description": _delegation_descriptions.PARAMS["spawn_session"]["mode"].text,
        },
        "narrowing": {
            "type": "object",
            "description": _delegation_descriptions.PARAMS["spawn_session"]["narrowing"].text,
        },
        "base_dir": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["spawn_session"]["base_dir"].text,
        },
        "agent": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["spawn_session"]["agent"].text,
        },
        "session": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["spawn_session"]["session"].text,
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
            "spawn_session requires ctx.router_state.spawn_session_fn — unavailable "
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
        base_dir=args.get("base_dir"),
        agent=args.get("agent"), session=args.get("session"),
    )


from reyn.core.offload.canonical import session_spawn_to_canonical  # noqa: E402

SESSION_SPAWN = ToolDefinition(
    canonical=session_spawn_to_canonical,
    name="spawn_session",
    router_dispatched=True,
    description=_SESSION_SPAWN_DESCRIPTION,
    parameters=_SESSION_SPAWN_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="async",  # the spawned session runs the task; result not returned inline
)
