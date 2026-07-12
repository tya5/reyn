"""mcp_install ToolDefinition (ADR-0026 + ADR-0029).

MCP_INSTALL_OP is phase-only (gates.router="deny", gates.phase="allow").
Install operations are gated at the phase level — not callable directly
from the router — to ensure proper registry lookup, permission gating,
and credential prompting are executed via op_runtime.

The handler delegates to op_runtime.mcp_install.handle, which performs:
  1. Registry fetch (RegistryClient.get_server)
  2. runtimeHint existence check
  3. Permission gate (PermissionResolver.require_mcp_install / ADR-0029)
  4. Secret env vars prompt + secrets.store persistence
  5. reyn.yaml / reyn.local.yaml write (scope-dependent)
  6. mcp_server_installed event emit (P6)
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import mcp as _mcp_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Reviewable in src/reyn/tools/descriptions/mcp.py (Phase 2 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_MCP_INSTALL_DESCRIPTION = _mcp_descriptions.mcp_install.text

_MCP_INSTALL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server_id": {
            "type": "string",
            "description": _mcp_descriptions.PARAMS["mcp_install"]["server_id"].text,
        },
        "scope": {
            "type": "string",
            "enum": ["local", "project", "user"],
            "description": _mcp_descriptions.PARAMS["mcp_install"]["scope"].text,
        },
        "env_overrides": {
            "type": "object",
            "description": _mcp_descriptions.PARAMS["mcp_install"]["env_overrides"].text,
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
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.mcp_install import handle as mcp_install_handle
    from reyn.schemas.models import MCPInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl

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

    # Build a minimal OpContext from ToolContext.
    # #571 collapse arc Phase 5: synthesize a PermissionDecl that
    # declares the explicit list axes the op handler now requires
    # (= file.write on the canonical config path + http.get for
    # the registry host). Pre-approves via session so the runtime
    # require_file_write check passes silently — the tool itself
    # was already authorised at the permission gate level.
    canonical_config = ".reyn/config/mcp.yaml"
    registry_host = "registry.modelcontextprotocol.io"
    synth_decl = PermissionDecl(
        file_write=[{"path": canonical_config, "scope": "just_path"}],
        http_get=[{"host": registry_host}],
        # #571 Phase 6: wildcard authorises the op handler to save
        # the user-prompted secret values for whichever env vars
        # the registry declares as ``isSecret`` at runtime.
        secret_write=["*"],
    )
    if ctx.permission_resolver is not None:
        ctx.permission_resolver.session_approve_path(
            canonical_config, "mcp_install", "file.write",
        )
    legacy_ctx = OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=synth_decl,
        permission_resolver=ctx.permission_resolver,
        actor="mcp_install",
        state_log=getattr(ctx, "state_log", None),  # #2259 PR-1: config generation emit
    )

    return await mcp_install_handle(op=op, ctx=legacy_ctx)


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

MCP_INSTALL_OP = ToolDefinition(
    canonical=STRUCTURED_PASSTHROUGH,
    name="mcp_install",
    description=_MCP_INSTALL_DESCRIPTION,
    parameters=_MCP_INSTALL_PARAMETERS,
    gates=ToolGates(router="deny", phase="allow"),
    handler=_handle_mcp_install_op,
    category="io",
    purity="side_effect",
    # proposal 0060 D5d: mirrors the "mcp" PartTypeSpec's doc_ref
    # (reyn.core.part_types.mcp) — same part-type, install-op axis.
    doc_ref="docs/concepts/tools-integrations/mcp.md",
)
