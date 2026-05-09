"""mcp_install ToolDefinition (ADR-0026 + ADR-0029).

MCP_INSTALL_OP is phase-only (gates.router="deny", gates.phase="allow").
Install operations must flow through the mcp_install skill — not from
the router directly — to ensure proper registry lookup, permission gating,
and credential prompting are executed in a structured skill context.

The handler delegates to op_runtime.mcp_install.handle, which performs:
  1. Registry fetch (RegistryClient.get_server)
  2. runtimeHint existence check
  3. Permission gate (PermissionResolver.require_mcp_install / ADR-0029)
  4. Secret env vars prompt + secrets.store persistence
  5. reyn.yaml / .reyn/config.yaml write (scope-dependent)
  6. mcp_server_installed event emit (P6)
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_MCP_INSTALL_DESCRIPTION = (
    "Install an MCP server from the registry. "
    "Fetches server.json, gates via permission resolver, "
    "prompts for secrets, and writes the server entry to the "
    "appropriate scope config file (local / project / user)."
)

_MCP_INSTALL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server_id": {
            "type": "string",
            "description": (
                "Registry identifier, e.g. "
                "'io.github.modelcontextprotocol/server-filesystem'."
            ),
        },
        "scope": {
            "type": "string",
            "enum": ["local", "project", "user"],
            "description": (
                "Config tier to write the server entry to. "
                "'local' → .reyn/config.yaml (default), "
                "'project' → reyn.yaml, "
                "'user' → ~/.reyn/config.yaml."
            ),
        },
        "env_overrides": {
            "type": "object",
            "description": (
                "Pre-supplied env values for secret vars required by the server. "
                "Keys are env var names; values are the secrets. "
                "Values not provided here will be prompted interactively."
            ),
            "additionalProperties": {"type": "string"},
        },
    },
    "required": ["server_id"],
}


async def _handle_mcp_install_op(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Phase-side handler for mcp_install op.

    Builds an MCPInstallIROp from args and dispatches through
    op_runtime.mcp_install.handle, which owns the full install lifecycle.
    """
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.mcp_install import handle as mcp_install_handle
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import MCPInstallIROp

    server_id = str(args["server_id"])
    scope_raw = args.get("scope", "local")
    scope = scope_raw if scope_raw in ("local", "project", "user") else "local"
    env_overrides_raw = args.get("env_overrides") or {}
    env_overrides = {str(k): str(v) for k, v in env_overrides_raw.items()}

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id=server_id,
        scope=scope,
        env_overrides=env_overrides or None,
    )

    # Obtain or build OpContext from ToolContext.
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
            permission_decl=PermissionDecl(mcp_install=True),
            permission_resolver=ctx.permission_resolver,
            skill_name="",
        )

    return await mcp_install_handle(op=op, ctx=legacy_ctx, caller="control_ir")


MCP_INSTALL_OP = ToolDefinition(
    name="mcp_install",
    description=_MCP_INSTALL_DESCRIPTION,
    parameters=_MCP_INSTALL_PARAMETERS,
    gates=ToolGates(router="deny", phase="allow"),
    handler=_handle_mcp_install_op,
    category="io",
    purity="side_effect",
)
