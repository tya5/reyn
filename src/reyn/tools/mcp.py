"""mcp_* ToolDefinitions — Wave 2 of M3 (ADR-0026 M3) + Type C closure.

Four capabilities are registered here (MCP_OP coarse ToolDef dropped in
#1240 Wave 2b — see end of file):

  CALL_MCP_TOOL    — gates.router=allow, gates.phase=allow
  LIST_MCP_SERVERS — gates.router=allow, gates.phase=allow  (Type C closure)
  LIST_MCP_TOOLS   — gates.router=allow, gates.phase=allow  (Type C closure)
  DESCRIBE_MCP_TOOL — gates.router=allow, gates.phase=allow (FP-0032 D4)

Per ADR-0026 Open Q #6, router-side fine-grained names are canonical:
call_mcp_tool / list_mcp_servers / list_mcp_tools / describe_mcp_tool.

## Phase-side dispatch status (post-#1240 Wave 2b)

All four ToolDefinitions have gates.phase="allow".  The coarse MCP_OP
ToolDef (kind="mcp") is dropped; phase advertises "call_mcp_tool" via
available_ops() and the (A)-alias rewrites it to "mcp" at the parse
boundary.  Dispatch falls to op_runtime/mcp.py via execute_op fallback.

The fine-grained discovery names (``list_mcp_servers`` / ``list_mcp_tools``
/ ``describe_mcp_tool``) are NOT in ``OP_KIND_MODEL_MAP``, so phase Control
IR cannot emit them today.  Their phase=allow gate is a metadata closure:
the registry advertises them as phase-eligible (consumed by future
``render_for_phase()`` enumerations), but the phase paths in the handlers
below are unreachable until a separate FP migrates Control IR schema to
fine-grained ``op.kind`` values.  The handlers therefore keep clear
"not yet wired" error stubs as defensive guards for any caller that
manages to bypass the schema-level rejection.

## Router-side dispatch

The router-side handlers are thin adapters over the existing session-level
callbacks (mcp_list_servers / mcp_list_tools / mcp_call_tool). The ToolContext
router_state carries the host adapter; adapters pull from ctx.router_state.

## DO NOT TOUCH shared files

Per task spec: __init__.py, router_tools.py, and registry.py are NOT modified
by this file. Registration of these 3 ToolDefinitions into get_default_registry()
is handled by the caller per ADR-0026 M3 wave pattern.
"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Final, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

if TYPE_CHECKING:
    from reyn.tools.types import RouterCallerState

# ── Description constants (byte-identical to router_tools.py D1/D2/D3) ────────

_LIST_MCP_SERVERS_DESCRIPTION = (
    "List available MCP servers configured for this agent. "
    "Returns name + description per server."
)

_LIST_MCP_TOOLS_DESCRIPTION = (
    "List tools exposed by one MCP server "
    "(with description per tool)."
)

_CALL_MCP_TOOL_DESCRIPTION = (
    "Invoke a mcp_tool on an MCP server. Construct args matching "
    "the mcp_tool's input schema (see describe_mcp_tool)."
)

_DESCRIBE_MCP_TOOL_DESCRIPTION = (
    "Get the input schema for one mcp_tool registered on an MCP server. "
    "Call this before call_mcp_tool if you're unsure how to "
    "construct the args."
)


# ── Parameters JSON schemas (byte-identical to router_tools.py D1/D2/D3) ──────

_LIST_MCP_SERVERS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}

_LIST_MCP_TOOLS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": {"type": "string"},
    },
    "required": ["server"],
}

# #1646: the target MCP tool's OWN parameters are carried under THIS key —
# deliberately NOT "args". The universal-scheme live path wraps this verb in
# invoke_action(action_name="mcp__call_tool", args={...}); a nested "args" here would
# collide with invoke_action's own "args" (two same-named levels), which the LLM
# collapsed (params flat beside server/mcp_tool_name, inner level dropped) → empty args
# at the MCP call (owner-observed). A distinct key kills the collision by construction.
# Single-sourced so the schema decl + both read sites (router + phase) cannot drift.
_MCP_TOOL_ARGS_KEY: Final[str] = "tool_args"

_CALL_MCP_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": {
            "type": "string",
            "description": "MCP server name — choose from the enum (verbatim).",
        },
        "mcp_tool_name": {
            "type": "string",
            "description": (
                "Dotted mcp_tool identifier: <server>.<tool> — choose from "
                "the enum. Use describe_mcp_tool for the full input schema."
            ),
        },
        _MCP_TOOL_ARGS_KEY: {
            "type": "object",
            "description": (
                "The target MCP tool's OWN parameters (the shape from "
                "describe_mcp_tool), as a nested object here — NOT flat alongside "
                "server / mcp_tool_name."
            ),
        },
    },
    "required": ["server", "mcp_tool_name", _MCP_TOOL_ARGS_KEY],
}

_DESCRIBE_MCP_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": {
            "type": "string",
            "description": "MCP server name — choose from the enum (verbatim).",
        },
        "mcp_tool_name": {
            "type": "string",
            "description": (
                "Dotted mcp_tool identifier: <server>.<tool> — choose from "
                "the enum."
            ),
        },
    },
    "required": ["server", "mcp_tool_name"],
}


# ── Handlers ──────────────────────────────────────────────────────────────────

async def _handle_list_mcp_servers(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for list_mcp_servers.

    Router path: delegates to host.mcp_list_servers() via ctx.router_state.
    Phase path: registered with gates.phase=allow (Type C metadata closure)
    but unreachable today — phase Control IR emits only coarse op.kind
    values defined in OP_KIND_MODEL_MAP, and ``list_mcp_servers`` is not
    in that map.  See module docstring for the full status.

    The router_state is expected to carry a host object with an async
    mcp_list_servers() method (= RouterHostAdapter or compatible).
    """
    if ctx.caller_kind == "router":
        host = _require_host(ctx)
        result = await host.mcp_list_servers()
        return {"servers": result}

    # Phase path — unreachable in normal flow; defensive guard for any
    # caller that bypasses Control IR schema rejection.
    return {
        "error": (
            "list_mcp_servers has no phase-side dispatch path. "
            "Phase Control IR emits only coarse op.kind values; "
            "fine-grained MCP discovery from phase requires a separate "
            "Control IR schema migration (out of scope for ADR-0026 M4)."
        )
    }


async def _handle_list_mcp_tools(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for list_mcp_tools.

    Router path: delegates to host.mcp_list_tools(server) via ctx.router_state.
    Phase path: registered with gates.phase=allow (Type C metadata closure)
    but unreachable today — see module docstring for full status.

    Response shape: ``{"mcp_tools": [{"name": "<server>__<tool>",
    "description": "...", "inputSchema": {...}}, ...]}``.

    Background:
      - FP-0032 returned ``mcp_tools`` key (not ``tools``) to avoid
        structural collision with OpenAI tool-definition shape, and
        also stripped ``inputSchema`` so the entries could not be
        mistaken for top-level callable functions.
      - Issue #879 collapsed MCP dispatch into a single
        ``mcp__call_tool`` verb whose ``tool`` arg takes a
        ``<server>__<tool>`` self-contained identifier. In that
        world the entry name is **not** a callable function name in
        the router's ``tools=`` array, so the FP-0032 shape-collision
        concern no longer applies — and the LLM needs the schema
        directly to construct ``mcp__call_tool``'s ``args`` field
        without an extra ``describe_mcp_tool`` round-trip. Include
        ``inputSchema`` in each entry verbatim from the MCP server's
        declared shape.
    """
    if ctx.caller_kind == "router":
        host = _require_host(ctx)
        server = str(args["server"])
        result = await host.mcp_list_tools(server)
        # Issue #879: rewrite each entry's ``name`` to the
        # ``<server>__<tool>`` identifier; preserve description + the
        # tool's declared ``inputSchema`` so the LLM can construct
        # mcp__call_tool args in a single follow-up turn.
        rebuilt: list[dict] = []
        for t in (result or []):
            if not isinstance(t, Mapping):
                continue
            if "error" in t:
                # Surface MCP-layer errors so the LLM can diagnose the failure
                # instead of seeing an empty tool list with no explanation.
                # Return without "mcp_tools" key so _normalise_router_tool_result
                # passes the dict through verbatim rather than unwrapping it.
                return {"error": t["error"]}
            inner_name = t.get("name", "")
            if not inner_name:
                continue
            entry = dict(t)
            entry["name"] = f"{server}__{inner_name}"
            rebuilt.append(entry)
        return {"mcp_tools": rebuilt}

    return {
        "error": (
            "list_mcp_tools has no phase-side dispatch path. "
            "Phase Control IR emits only coarse op.kind values; "
            "fine-grained MCP discovery from phase requires a separate "
            "Control IR schema migration (out of scope for ADR-0026 M4)."
        )
    }


async def _handle_call_mcp_tool(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for call_mcp_tool.

    Router path: delegates to host.mcp_call_tool(server, tool, args) via
    ctx.router_state. This preserves the existing router_loop.py dispatch
    semantics (= session._mcp_call_tool → execute_op(MCPIROp, ctx)).

    Phase path: fully wired. Builds a MCPIROp from args and dispatches
    through op_runtime.mcp.handle(), which performs permission gating and
    MCPClient invocation. This is the existing "mcp" kind handler — the
    ToolDefinition name "call_mcp_tool" maps to MCPIROp(kind="mcp", ...).

    Polymorphic args handling: MCPIROp accepts arbitrary server/tool/args,
    making the dynamic MCP tool space available without OS-level changes
    (P7 compliance — no skill-specific strings in OS code).
    """
    if ctx.caller_kind == "router":
        host = _require_host(ctx)
        server = str(args["server"])
        mcp_tool_name = str(args["mcp_tool_name"])
        # Dotted form "server.tool_name" → extract the bare tool name for MCPClient.
        # If the caller passed a bare name (no dot), use it as-is for compatibility.
        bare_tool = mcp_tool_name.split(".", 1)[-1] if "." in mcp_tool_name else mcp_tool_name
        tool_args = dict(args.get(_MCP_TOOL_ARGS_KEY) or {})  # #1646: distinct key, no invoke_action collision
        return await host.mcp_call_tool(server, bare_tool, tool_args)

    # Phase path: build MCPIROp and dispatch through op_runtime.
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.mcp import handle as mcp_handle
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import MCPIROp

    server = str(args["server"])
    mcp_tool_name = str(args["mcp_tool_name"])
    # Dotted form → extract bare tool name for MCPIROp.
    tool = mcp_tool_name.split(".", 1)[-1] if "." in mcp_tool_name else mcp_tool_name
    tool_args = dict(args.get(_MCP_TOOL_ARGS_KEY) or {})  # #1646: distinct key, no invoke_action collision

    op = MCPIROp(kind="mcp", server=server, tool=tool, args=tool_args)

    # Build a legacy OpContext from the new ToolContext.
    # phase_state.op_context carries the full OpContext when wired by the phase
    # dispatcher (M4 Phase 3). Fall back to a minimal context for direct
    # (non-phase-dispatcher) calls.
    _op_ctx = (
        ctx.phase_state.op_context
        if ctx.phase_state is not None
        else None
    )
    if _op_ctx is not None and isinstance(_op_ctx, OpContext):
        legacy_ctx = _op_ctx
    else:
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(mcp=[server]),
            permission_resolver=ctx.permission_resolver,
            skill_name="",
            skill=None,
            model="standard",
            resolver=None,
            subscribers=getattr(ctx.events, "subscribers", []),
            output_language=None,
            max_phase_visits=25,
            sub_state_dir_override=None,
            state_dir_strategy="control_ir",
            mcp_servers={},
            mcp_clients={},
            intervention_bus=None,
            current_phase="",
            caller="direct",
            parent_skill_run_id=None,
        )

    return await mcp_handle(op=op, ctx=legacy_ctx, caller="control_ir")


# ── Private helpers ───────────────────────────────────────────────────────────

def _require_host(ctx: ToolContext) -> Any:
    """Extract host from ctx.router_state.host, raising if absent.

    Production wiring (Phase 3.5-B-mid): RouterLoop sets
    ``ctx.router_state.host`` to the RouterHostAdapter instance so MCP
    handlers can call ``host.mcp_list_servers()`` etc. directly,
    preserving the existing session-level mcp_clients cache (= no
    re-handshake per call).

    Backward-compat: pre-Phase-3-step-2 tests that assigned
    ``ctx.router_state = some_host_stub`` (= router_state IS the host
    duck-type, not a RouterCallerState) still work via the duck-type
    fallback below.
    """
    rs = ctx.router_state
    if rs is None:
        raise RuntimeError(
            "MCP tool handlers require ctx.router_state.host to carry the "
            "RouterHostAdapter (set by the router dispatcher before calling "
            "the handler). router_state is None — this is a dispatcher wiring bug."
        )
    # Phase 3.5+ path: typed RouterCallerState with .host populated.
    host = getattr(rs, "host", None)
    if host is not None:
        return host
    # Backward-compat: pre-typed router_state = host stub.
    if hasattr(rs, "mcp_list_servers"):
        return rs
    raise RuntimeError(
        "MCP tool handlers require ctx.router_state.host to carry the "
        "RouterHostAdapter (Phase 3.5-B-mid wiring), or for the legacy "
        "router_state = host stub pattern, the stub must expose "
        "mcp_list_servers / mcp_list_tools / mcp_call_tool methods."
    )


# ── FP-0032: Schema enricher for call_mcp_tool / describe_mcp_tool ───────────


def _enrich_router_schema(rendered: dict, state: "RouterCallerState") -> dict:
    """Inject server + mcp_tool_name enums from currently-configured MCP servers.

    The enum lists are dynamic: they depend on which MCP servers are wired into
    the current chat session (= reyn.yaml `mcp` config + per-server tool listings).
    Without these enums, the LLM could emit arbitrary string values for
    ``server`` and ``mcp_tool_name``, leading to runtime "unknown server" errors
    or the FP-0032 bug (LLM emits a bare mcp_tool_name as if it were a
    top-level tool call).

    ``mcp_servers`` entries: [{name, description, ...}, ...] — may optionally
    carry a ``tools`` list [{name, ...}, ...] for tool-level enum injection.
    When ``tools`` is absent (common: tool listing requires async enumeration),
    the mcp_tool_name enum is omitted and the field stays a plain string.

    Returns a NEW dict — does not mutate the input.
    """
    mcp_servers = state.mcp_servers or []
    server_names = [str(s["name"]) for s in mcp_servers if "name" in s]
    mcp_tool_names = [
        f"{s['name']}.{t['name']}"
        for s in mcp_servers
        for t in s.get("tools", [])
        if "name" in s and "name" in t
    ]
    new = copy.deepcopy(rendered)
    props = new["function"]["parameters"]["properties"]
    server_prop = props.get("server")
    mcp_tool_prop = props.get("mcp_tool_name")
    if server_prop is not None:
        if server_names:
            server_prop["enum"] = server_names
        else:
            server_prop.pop("enum", None)
    if mcp_tool_prop is not None:
        if mcp_tool_names:
            mcp_tool_prop["enum"] = mcp_tool_names
        else:
            mcp_tool_prop.pop("enum", None)
    return new


# ── FP-0032 D4: describe_mcp_tool handler ────────────────────────────────────


async def _handle_describe_mcp_tool(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Return {name, description, input_schema} for the requested mcp_tool.

    Router path: calls host.mcp_list_tools(server) to get the tool listing,
    then filters to the requested mcp_tool_name. The dotted form
    ``<server>.<tool>`` is resolved to the bare tool name for the lookup.

    Phase path: metadata closure only — same as list_mcp_tools. See module
    docstring for full status.
    """
    if ctx.caller_kind == "router":
        host = _require_host(ctx)
        server = str(args["server"])
        mcp_tool_name = str(args["mcp_tool_name"])
        bare_tool = mcp_tool_name.split(".", 1)[-1] if "." in mcp_tool_name else mcp_tool_name
        all_tools = await host.mcp_list_tools(server) or []
        for t in all_tools:
            if str(t.get("name", "")) == bare_tool:
                return {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "input_schema": t.get("inputSchema", {}),
                }
        return {
            "error": (
                f"mcp_tool {mcp_tool_name!r} not found on server {server!r}. "
                "Use list_mcp_tools to see available mcp_tools."
            )
        }

    return {
        "error": (
            "describe_mcp_tool has no phase-side dispatch path. "
            "Phase Control IR emits only coarse op.kind values; "
            "fine-grained MCP discovery from phase requires a separate "
            "Control IR schema migration (out of scope for ADR-0026 M4)."
        )
    }


# ── ToolDefinitions ───────────────────────────────────────────────────────────

LIST_MCP_SERVERS = ToolDefinition(
    name="list_mcp_servers",
    description=_LIST_MCP_SERVERS_DESCRIPTION,
    parameters=_LIST_MCP_SERVERS_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_list_mcp_servers,
    category="discovery",
    purity="read_only",
)

LIST_MCP_TOOLS = ToolDefinition(
    name="list_mcp_tools",
    description=_LIST_MCP_TOOLS_DESCRIPTION,
    parameters=_LIST_MCP_TOOLS_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_list_mcp_tools,
    category="discovery",
    purity="read_only",
)

CALL_MCP_TOOL = ToolDefinition(
    name="call_mcp_tool",
    description=_CALL_MCP_TOOL_DESCRIPTION,
    parameters=_CALL_MCP_TOOL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_call_mcp_tool,
    category="discovery",
    purity="side_effect",  # call_mcp_tool has arbitrary side effects
    schema_enricher=_enrich_router_schema,
)

DESCRIBE_MCP_TOOL = ToolDefinition(
    name="describe_mcp_tool",
    description=_DESCRIBE_MCP_TOOL_DESCRIPTION,
    parameters=_DESCRIBE_MCP_TOOL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_describe_mcp_tool,
    category="discovery",
    purity="read_only",
    schema_enricher=_enrich_router_schema,
)


# #1240 Wave 2b: MCP_OP (the coarse phase-side ToolDefinition under the name
# "mcp") is DROPPED.  Phase Control IR now advertises the chat name
# "call_mcp_tool" via available_ops() (ControlIROpSpec with kind="call_mcp_tool"),
# which aliases to op kind "mcp" at the parse boundary.  Dispatch falls to the
# legacy execute_op path (op_runtime/mcp.py register("mcp")).
# allowed_ops=[mcp] continues to match the call_mcp_tool spec via
# _PHASE_TOOL_NAME_ALIAS in runtime.build_frame.
# KEPT: CALL_MCP_TOOL (router+phase, gates.phase="allow") is the canonical
# phase-advertised ToolDefinition.  The call_mcp_tool handler (phase path) builds
# MCPIROp and delegates to op_runtime.mcp.handle directly.
