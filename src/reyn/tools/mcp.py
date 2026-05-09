"""mcp_* ToolDefinitions — Wave 2 of M3 (ADR-0026 M3) + Type C closure.

Three capabilities are registered here:

  CALL_MCP_TOOL   — gates.router=allow, gates.phase=allow
  LIST_MCP_SERVERS — gates.router=allow, gates.phase=allow  (Type C closure)
  LIST_MCP_TOOLS  — gates.router=allow, gates.phase=allow  (Type C closure)

Per ADR-0026 Open Q #6, router-side fine-grained names are canonical:
call_mcp_tool / list_mcp_servers / list_mcp_tools.

## Phase-side dispatch status (M3 metadata-only)

All three ToolDefinitions have gates.phase="allow", which closes the
Type C gap at the *metadata* level: the registry declares them available
to phase, and render_for_phase() will include them in available_control_ops.

However, the phase-side Control IR executor does not yet consume the
unified registry for dispatch. Phase-side wiring for list_mcp_servers and
list_mcp_tools is deferred to M4 (the op_runtime dispatcher currently only
handles the "mcp" kind via MCPIROp which maps to call_mcp_tool semantics).

CALL_MCP_TOOL is already wired end-to-end because it maps 1-to-1 to the
existing op_runtime/mcp.py "mcp" kind handler via MCPIROp.

For LIST_MCP_SERVERS and LIST_MCP_TOOLS: the ToolDefinition is registered
with gates.phase="allow" (closing the Type C metadata gap), but phase-side
Control IR execution of these ops is not yet plumbed — a phase that emits
list_mcp_servers / list_mcp_tools control_ir ops will not be dispatched by
the current executor. That dispatch wiring is the M4 task.

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

from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


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
    "Invoke an MCP server tool. Construct args matching "
    "the tool's input schema (see list_mcp_tools)."
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

_CALL_MCP_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": {"type": "string"},
        "tool": {"type": "string"},
        "args": {"type": "object"},
    },
    "required": ["server", "tool", "args"],
}


# ── Handlers ──────────────────────────────────────────────────────────────────

async def _handle_list_mcp_servers(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for list_mcp_servers.

    Router path: delegates to host.mcp_list_servers() via ctx.router_state.
    Phase path: registered with gates.phase=allow (Type C metadata closure),
    but phase-side Control IR executor wiring is deferred to M4.

    The router_state is expected to carry a host object with an async
    mcp_list_servers() method (= RouterHostAdapter or compatible).
    """
    if ctx.caller_kind == "router":
        host = _require_host(ctx)
        result = await host.mcp_list_servers()
        return {"servers": result}

    # Phase path — M4 will wire phase-side execution; for now surface a
    # clear error so any premature phase invocation is immediately visible.
    return {
        "error": (
            "list_mcp_servers phase-side dispatch not yet wired "
            "(M4 task). ToolDefinition registered for metadata closure only."
        )
    }


async def _handle_list_mcp_tools(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for list_mcp_tools.

    Router path: delegates to host.mcp_list_tools(server) via ctx.router_state.
    Phase path: registered with gates.phase=allow (Type C metadata closure),
    but phase-side Control IR executor wiring is deferred to M4.
    """
    if ctx.caller_kind == "router":
        host = _require_host(ctx)
        server = str(args["server"])
        result = await host.mcp_list_tools(server)
        return {"tools": result}

    return {
        "error": (
            "list_mcp_tools phase-side dispatch not yet wired "
            "(M4 task). ToolDefinition registered for metadata closure only."
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
        tool = str(args["tool"])
        tool_args = dict(args.get("args") or {})
        return await host.mcp_call_tool(server, tool, tool_args)

    # Phase path: build MCPIROp and dispatch through op_runtime.
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.mcp import handle as mcp_handle
    from reyn.schemas.models import MCPIROp
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    server = str(args["server"])
    tool = str(args["tool"])
    tool_args = dict(args.get("args") or {})

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
            shell_allowed=False,
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
    """Extract host from ctx.router_state, raising if absent.

    The router_state is set by the router dispatcher before calling
    tool handlers. For MCP tools, it must carry a host object with
    mcp_list_servers / mcp_list_tools / mcp_call_tool async methods.
    """
    host = ctx.router_state
    if host is None:
        raise RuntimeError(
            "MCP tool handlers require ctx.router_state to carry the "
            "RouterHostAdapter (set by the router dispatcher before calling "
            "the handler). router_state is None — this is a dispatcher wiring bug."
        )
    return host


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
)
