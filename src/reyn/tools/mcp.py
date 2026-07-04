"""mcp_* ToolDefinitions — Wave 2 of M3 (ADR-0026 M3).

Four capabilities are registered here (MCP_OP coarse ToolDef dropped in
#1240 Wave 2b — see end of file):

  CALL_MCP_TOOL    — gates.router=allow
  LIST_MCP_SERVERS — gates.router=allow
  LIST_MCP_TOOLS   — gates.router=allow
  DESCRIBE_MCP_TOOL — gates.router=allow (FP-0032 D4)

Per ADR-0026 Open Q #6, router-side fine-grained names are canonical:
call_mcp_tool / list_mcp_servers / list_mcp_tools / describe_mcp_tool.

All four handlers dispatch through the router path only. The phase-side
dispatch branches were removed alongside the control-IR / phase-dispatch
executor (#2542): ``ToolContext.caller_kind`` is always "router" at
runtime, so the handlers run their router logic unconditionally.

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

    Delegates to host.mcp_list_servers() via ctx.router_state. The
    router_state is expected to carry a host object with an async
    mcp_list_servers() method (= RouterHostAdapter or compatible).
    """
    host = _require_host(ctx)
    result = await host.mcp_list_servers()
    return {"servers": result}


async def _handle_list_mcp_tools(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for list_mcp_tools.

    Delegates to host.mcp_list_tools(server) via ctx.router_state.

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


async def _handle_call_mcp_tool(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Adapter for call_mcp_tool.

    Delegates to host.mcp_call_tool(server, tool, args) via
    ctx.router_state. This preserves the existing router_loop.py dispatch
    semantics (= session._mcp_call_tool → execute_op(MCPIROp, ctx)).
    """
    host = _require_host(ctx)
    server = str(args["server"])
    mcp_tool_name = str(args["mcp_tool_name"])
    # Dotted form "server.tool_name" → extract the bare tool name for MCPClient.
    # If the caller passed a bare name (no dot), use it as-is for compatibility.
    bare_tool = mcp_tool_name.split(".", 1)[-1] if "." in mcp_tool_name else mcp_tool_name
    tool_args = dict(args.get(_MCP_TOOL_ARGS_KEY) or {})  # #1646: distinct key, no invoke_action collision
    return await host.mcp_call_tool(server, bare_tool, tool_args)


# ── Private helpers ───────────────────────────────────────────────────────────

def _require_host(ctx: ToolContext) -> Any:
    """Extract host from ctx.router_state.host, raising if absent.

    Production wiring (Phase 3.5-B-mid): RouterLoop sets
    ``ctx.router_state.host`` to the RouterHostAdapter instance so MCP
    handlers can call ``host.mcp_list_servers()`` etc. directly. #2567:
    ``build_resource_caller_state`` populates the same field for any
    host-holding caller (e.g. a pipeline driver-session), not only a live
    RouterLoop turn. Note the MCP client pool is per-call
    (``session.py``'s ``_mcp_call_tool`` opens a fresh ``MCPClientPool``
    per invocation) — there is no session-level connection cache here to
    preserve; ``router_state.host`` is just the resource-lookup seam.

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

    Calls host.mcp_list_tools(server) to get the tool listing, then
    filters to the requested mcp_tool_name. The dotted form
    ``<server>.<tool>`` is resolved to the bare tool name for the lookup.
    """
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


# ── ToolDefinitions ───────────────────────────────────────────────────────────

LIST_MCP_SERVERS = ToolDefinition(
    name="list_mcp_servers",
    router_dispatched=True,
    description=_LIST_MCP_SERVERS_DESCRIPTION,
    parameters=_LIST_MCP_SERVERS_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_list_mcp_servers,
    category="discovery",
    purity="read_only",
)

LIST_MCP_TOOLS = ToolDefinition(
    name="list_mcp_tools",
    router_dispatched=True,
    description=_LIST_MCP_TOOLS_DESCRIPTION,
    parameters=_LIST_MCP_TOOLS_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),  # Type C closure
    handler=_handle_list_mcp_tools,
    category="discovery",
    purity="read_only",
    returns_external_content=True,  # FP-0050/#1822: external server-authored tool descriptions
)

CALL_MCP_TOOL = ToolDefinition(
    name="call_mcp_tool",
    router_dispatched=True,
    description=_CALL_MCP_TOOL_DESCRIPTION,
    parameters=_CALL_MCP_TOOL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_call_mcp_tool,
    category="discovery",
    purity="side_effect",  # call_mcp_tool has arbitrary side effects
    returns_external_content=True,  # FP-0050/#1822: external MCP server result
    schema_enricher=_enrich_router_schema,
)

DESCRIBE_MCP_TOOL = ToolDefinition(
    name="describe_mcp_tool",
    router_dispatched=True,
    description=_DESCRIBE_MCP_TOOL_DESCRIPTION,
    parameters=_DESCRIBE_MCP_TOOL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_describe_mcp_tool,
    category="discovery",
    purity="read_only",
    returns_external_content=True,  # FP-0050/#1822: external server-authored schema/description
    schema_enricher=_enrich_router_schema,
)


# #1240 Wave 2b: the coarse MCP_OP ToolDefinition (kind="mcp") is DROPPED.
# The router advertises the fine-grained name "call_mcp_tool"; its handler
# delegates to host.mcp_call_tool (session._mcp_call_tool → execute_op on the
# op_runtime "mcp" kind). The op_runtime.mcp.handle op-kind handler stays live —
# it is still invoked by that router dispatch path and by external_routing.py.
