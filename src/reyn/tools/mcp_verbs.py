"""MCP verb-object handlers — collapsed surface (#879).

This module implements the six router-callable MCP verbs the LLM sees
under the single ``mcp`` category:

  - ``mcp__search_server``  — registry search (pure op-runtime, no skill)
  - ``mcp__install_server`` — install a server (pure op-runtime; secrets
    via ``reyn secret set``, no mid-flight ask_user)
  - ``mcp__list_servers``   — list installed servers (existing
    ``LIST_MCP_SERVERS``)
  - ``mcp__list_tools``     — list a server's tools as
    ``<server>__<tool>`` identifiers (existing ``LIST_MCP_TOOLS``,
    return shape updated for the new identifier convention)
  - ``mcp__call_tool``      — call a tool by ``<server>__<tool>``
    identifier (this module)
  - ``mcp__drop_server``    — remove an installed server (existing
    ``MCP_DROP_SERVER``)

No skills are spawned. The previous stdlib ``mcp_search`` / ``mcp_install``
skills are removed in this PR; both flows live in op-runtime handlers
called directly from the verb ToolDefinitions below.

Secret handling for ``mcp__install_server`` is **strict args + guide**:
when the registry's package metadata declares ``isSecret: true``
env-vars and the operator has not pre-supplied them (via
``env_overrides`` or ``reyn secret set``), the install short-circuits
with a ``status: "needs_secrets"`` result whose ``guide`` field tells
the operator which keys to set. The LLM forwards that guide; the
operator sets the secrets through ``reyn secret set <KEY>``; the LLM
retries ``mcp__install_server``.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── mcp__search_server ────────────────────────────────────────────────────────


_MCP_SEARCH_SERVER_DESCRIPTION = (
    "Search the MCP registry for servers relevant to a natural-language "
    "capability request. Returns candidates with id / name / description / "
    "runtime_hint; pick one and follow up with mcp__install_server passing "
    "the chosen server_id. Multilingual — accepts queries in any language."
)

_MCP_SEARCH_SERVER_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "Natural-language capability request (e.g. \"github "
                "related\", \"image generation\", \"PDF を扱える\")."
            ),
        },
    },
    "required": ["text"],
}


def _extract_keyword(text: str) -> str:
    """Best-effort English keyword extraction.

    First run of ASCII letters ≥ 3 chars wins (e.g. embedded English
    product names inside mixed-language text). Falls back to the first
    whitespace token lowercased.
    """
    match = re.search(r"[A-Za-z]{3,}", text)
    if match:
        return match.group(0).lower()
    token = text.split()[0] if text.split() else text
    return token.lower()


async def _handle_mcp_search_server(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Pure op-runtime registry search — no skill spawn, no ask_user."""
    text = str(args.get("text", "") or "")
    if not text.strip():
        return {
            "status": "error",
            "data": {"error": "text is required"},
        }

    from reyn.safe.mcp.registry import RegistryError, search

    query = _extract_keyword(text)
    try:
        candidates = search(query, limit=20)
    except RegistryError as exc:
        return {
            "status": "error",
            "data": {
                "query": query,
                "candidates": [],
                "error": str(exc),
            },
        }

    return {
        "status": "ok",
        "data": {
            "query": query,
            "candidates": candidates,
        },
    }


# ── mcp__install_server ───────────────────────────────────────────────────────


_MCP_INSTALL_SERVER_DESCRIPTION = (
    "Install an MCP server from the registry into the current project "
    "configuration. Strict args — pass the server_id obtained from "
    "mcp__search_server (or a --source specifier). When the server "
    "requires secret environment variables that the operator has not "
    "yet set, the call returns status='needs_secrets' with a guide "
    "explaining the `reyn secret set <KEY>` command; relay that to the "
    "user and retry after they confirm secrets are set."
)

_MCP_INSTALL_SERVER_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server_id": {
            "type": "string",
            "description": (
                "Registry identifier, e.g. "
                "'io.github.modelcontextprotocol/server-time'. Empty when "
                "using 'source' instead."
            ),
        },
        "source": {
            "type": "string",
            "description": (
                "Optional --source specifier when not using the registry "
                "(e.g. 'pypi:mcp-server-time', 'npm:@org/pkg', GitHub URL). "
                "Mutually exclusive with server_id-only registry lookup."
            ),
        },
        "scope": {
            "type": "string",
            "enum": ["local", "project", "user"],
            "description": (
                "Config tier to write the server entry to. Default 'local'."
            ),
        },
        "env_overrides": {
            "type": "object",
            "description": (
                "Pre-supplied env values for the server's secret env-vars. "
                "Skip when secrets are already stored via `reyn secret set`."
            ),
            "additionalProperties": {"type": "string"},
        },
    },
    "required": [],
}


async def _handle_mcp_install_server(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Pure op-runtime install — delegates to mcp_install op handler.

    Secret handling: the op handler now returns a structured
    ``needs_secrets`` result when isSecret env-vars are not present,
    instead of prompting via ask_user. The LLM is expected to surface
    the guide to the operator and retry after secrets are set.
    """
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.mcp_install import handle as mcp_install_handle
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import MCPInstallIROp

    server_id = str(args.get("server_id") or "")
    source = args.get("source")
    if not server_id and not source:
        return {
            "status": "error",
            "data": {
                "error": (
                    "server_id or source is required. Call "
                    "mcp__search_server first to find candidates."
                ),
            },
        }

    try:
        op = MCPInstallIROp(
            kind="mcp_install",
            server_id=server_id,
            scope=args.get("scope", "local"),
            env_overrides=args.get("env_overrides"),
            source=source if isinstance(source, str) else None,
            extra_args=None,
        )
    except Exception as exc:
        return {
            "status": "error",
            "data": {"error": f"invalid args: {exc}"},
        }

    decl = PermissionDecl()
    decl.file_write = [{"path": ".reyn/mcp.yaml"}]
    decl.http_get = [{"host": "registry.modelcontextprotocol.io"}]
    decl.secret_write = ["*"]

    op_ctx = OpContext(
        skill_name="mcp__install_server",
        run_id=None,
        permission_decl=decl,
        permission_resolver=getattr(
            getattr(ctx, "router_state", None), "permission_resolver", None,
        ),
        events=getattr(ctx, "events", None),
        intervention_bus=None,
        media_store=None,
    )

    result = await mcp_install_handle(op, op_ctx, caller="control_ir")
    return {"status": "ok", "data": result}


# ── mcp__call_tool ────────────────────────────────────────────────────────────


_MCP_CALL_TOOL_DESCRIPTION = (
    "Call a tool on an installed MCP server. Pass the tool identifier "
    "in <server>__<tool> form (e.g. 'time__get_current_time') as "
    "returned by mcp__list_tools, plus the tool's own args dict."
)

_MCP_CALL_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {
            "type": "string",
            "description": (
                "<server>__<tool> identifier from mcp__list_tools "
                "(e.g. 'time__get_current_time')."
            ),
        },
        "args": {
            "type": "object",
            "description": "Per-tool args dict (consult mcp__list_tools).",
        },
    },
    "required": ["tool"],
}


async def _handle_mcp_call_tool(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Split ``<server>__<tool>`` identifier and dispatch to call_mcp_tool."""
    tool_id = str(args.get("tool") or "")
    if "__" not in tool_id:
        return {
            "status": "error",
            "data": {
                "error": (
                    f"tool identifier must have form '<server>__<tool>'; "
                    f"got {tool_id!r}"
                ),
            },
        }
    server, mcp_tool_name = tool_id.split("__", 1)
    if not server or not mcp_tool_name:
        return {
            "status": "error",
            "data": {
                "error": (
                    f"both <server> and <tool> must be non-empty; "
                    f"got {tool_id!r}"
                ),
            },
        }

    from reyn.tools.mcp import _handle_call_mcp_tool

    return await _handle_call_mcp_tool(
        {
            "server": server,
            "mcp_tool_name": mcp_tool_name,
            "args": dict(args.get("args") or {}),
        },
        ctx,
    )


# ── ToolDefinitions ──────────────────────────────────────────────────────────


MCP_SEARCH_SERVER = ToolDefinition(
    name="mcp_search_server",
    description=_MCP_SEARCH_SERVER_DESCRIPTION,
    parameters=_MCP_SEARCH_SERVER_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_mcp_search_server,
    category="discovery",
    purity="read_only",
)


MCP_INSTALL_SERVER = ToolDefinition(
    name="mcp_install_server",
    description=_MCP_INSTALL_SERVER_DESCRIPTION,
    parameters=_MCP_INSTALL_SERVER_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_mcp_install_server,
    category="io",
    purity="side_effect",
)


MCP_CALL_TOOL = ToolDefinition(
    name="mcp_call_tool",
    description=_MCP_CALL_TOOL_DESCRIPTION,
    parameters=_MCP_CALL_TOOL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_mcp_call_tool,
    category="io",
    purity="side_effect",
)


__all__ = ["MCP_SEARCH_SERVER", "MCP_INSTALL_SERVER", "MCP_CALL_TOOL"]
