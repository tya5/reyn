"""mcp_drop_server ToolDefinition (FP-0034 §D23).

MCP_DROP_SERVER_OP is the destructor counterpart to MCP_INSTALL_OP:
  - install adds an entry to ``mcp.servers.<short>``
  - drop_server removes that entry, optionally cleans secrets

Unlike mcp_install (which requires a multi-step skill flow due to
registry lookup + runtime detection + secret prompting), drop is
purely mechanical and can run as a single op invocation. Per
FP-0034 §D23, this op lives in the universal catalog under
``mcp.operation__drop_server`` and is reachable from both:

  - Router context (= LLM-driven removal via
    ``invoke_action("mcp.operation__drop_server", {server, ...})``)
  - Phase context (= Control IR op with kind="mcp_drop_server")

The handler delegates to op_runtime.mcp_drop_server.handle, which:
  1. Resolves scope (= explicit or auto-detect)
  2. Gates via require_mcp_drop_server (FP-0034 §D23 — distinct from
     mcp_install permission)
  3. Removes the YAML entry
  4. Optionally cleans secrets
  5. Emits mcp_server_removed P6 event
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_MCP_DROP_SERVER_DESCRIPTION = (
    "Remove a configured MCP server. "
    "Counter-op to mcp_install — deletes the server entry from "
    "reyn.local.yaml / reyn.yaml / ~/.reyn/config.yaml (scope is "
    "auto-detected when omitted). Optionally cleans the matching "
    "${KEY} env entries from ~/.reyn/secrets.env. "
    "Permission-gated via mcp_drop_server (= distinct from "
    "mcp_install; install intent alone is insufficient)."
)


_MCP_DROP_SERVER_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": {
            "type": "string",
            "description": (
                "Short server name as it appears under mcp.servers in "
                "configuration (e.g. 'filesystem', 'brave'). Use "
                "list_actions(category=['mcp.server']) to discover."
            ),
        },
        "scope": {
            "type": "string",
            "enum": ["local", "project", "user"],
            "description": (
                "Config tier to remove from. Omit to auto-detect by "
                "walking local → project → user and removing from the "
                "first match."
            ),
        },
        "clear_secrets": {
            "type": "boolean",
            "default": True,
            "description": (
                "When true (default), also remove the corresponding "
                "${KEY} entries from ~/.reyn/secrets.env. Set false to "
                "keep the secrets for reinstall."
            ),
        },
    },
    "required": ["server"],
}


async def _handle_mcp_drop_server_op(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Adapter wrapping op_runtime.mcp_drop_server.handle.

    Builds an MCPDropServerIROp from args and dispatches through
    op_runtime, which owns the full lifecycle (= scope detection,
    permission gate, yaml edit, secrets cleanup, P6 event emit).

    OpContext resolution mirrors drop_source / mcp_install:
      - Phase context: reuse ctx.phase_state.op_context when present
      - Router context: use ctx.router_state.op_context_factory when
        bound (= RouterLoop wires this with permission_decl populated)
      - Fallback: minimal OpContext with mcp_drop_server decl
    """
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import MCPDropServerIROp

    server = str(args["server"])
    scope_raw = args.get("scope")
    scope = scope_raw if scope_raw in ("local", "project", "user") else None
    clear_secrets = bool(args.get("clear_secrets", True))

    op = MCPDropServerIROp(
        kind="mcp_drop_server",
        server=server,
        scope=scope,
        clear_secrets=clear_secrets,
    )

    # Resolve OpContext — prefer caller-supplied, fall back to minimal.
    _op_ctx = (
        ctx.phase_state.op_context if ctx.phase_state is not None else None
    )
    if _op_ctx is not None and isinstance(_op_ctx, OpContext):
        legacy_ctx = _op_ctx
    elif (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(mcp_drop_server=True),
            permission_resolver=ctx.permission_resolver,
            skill_name="",
            intervention_bus=None,
            subscribers=getattr(ctx.events, "subscribers", []),
        )

    return await drop_handle(op=op, ctx=legacy_ctx, caller="control_ir")


MCP_DROP_SERVER_OP = ToolDefinition(
    name="mcp_drop_server",
    description=_MCP_DROP_SERVER_DESCRIPTION,
    parameters=_MCP_DROP_SERVER_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_mcp_drop_server_op,
    category="io",
    purity="side_effect",
)
