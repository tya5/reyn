"""MCP verb-object wrappers — surface collapse follow-up (#879).

Issue #879 collapses the previous ``mcp.server`` / ``mcp.tool`` /
``mcp.operation`` sub-categories + ``skill__mcp_search`` /
``skill__mcp_install`` hidden-in-skill-space actions into a single
``mcp`` category with six verb_object actions:

  - ``mcp__search_server`` — registry search (this module)
  - ``mcp__install_server`` — install a server (this module)
  - ``mcp__list_servers`` — list installed servers (existing
    ``LIST_MCP_SERVERS``)
  - ``mcp__list_tools`` — list tools of an installed server
    (existing ``LIST_MCP_TOOLS``)
  - ``mcp__call_tool`` — call a tool (existing ``CALL_MCP_TOOL``)
  - ``mcp__drop_server`` — remove an installed server (existing
    ``MCP_DROP_SERVER``)

The two ``*_server`` verbs need a skill spawn under the hood because
the install flow is multi-step (registry fetch → permission gate →
secret prompts → yaml write) and the search flow runs an HTTP +
filter pipeline through the existing ``mcp_search`` stdlib skill.
This module wraps them as router-callable ToolDefinitions that
forward to ``invoke_skill`` with a clean ``{text}`` schema so
``describe_action`` / hot-list alias surfacing produces the right
LLM-facing parameters.

Phase 1 (this PR): the wrappers delegate to the existing
``mcp_search`` / ``mcp_install`` skills so multi-step ``ask_user``
flows are preserved unchanged.

Phase 2 (deferred): collapse the skills into pure op_runtime
handlers once ``ask_user`` from a tool handler context is supported
end-to-end.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_MCP_SEARCH_SERVER_DESCRIPTION = (
    "Search the MCP registry for servers relevant to a natural-language "
    "capability request. Returns candidate server entries with id / name "
    "/ description; the LLM then chooses one and follows up with "
    "mcp__install_server. Multilingual — accepts queries in any language."
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


_MCP_INSTALL_SERVER_DESCRIPTION = (
    "Install an MCP server from the registry into the current project "
    "configuration. Accepts a free-text identifier (registry server id, "
    "package spec, or natural-language description); the underlying skill "
    "resolves it against the registry, gates via the permission resolver, "
    "prompts for secrets when needed, and writes the server entry into "
    "the appropriate scope's config file. Pair with mcp__search_server "
    "first when the exact server is not yet known."
)

_MCP_INSTALL_SERVER_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "Installation request — registry server id, package "
                "spec (e.g. 'pypi:mcp-server-time'), or natural language "
                "naming the server to install."
            ),
        },
    },
    "required": ["text"],
}


async def _handle_mcp_search_server(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Forward to the ``mcp_search`` stdlib skill via invoke_skill.

    The skill owns the multi-step search lifecycle (registry HTTP fetch
    + relevance filter); the wrapper just shapes the args.
    """
    from reyn.tools.invoke_skill import _handle as invoke_skill_handle

    return await invoke_skill_handle(
        {
            "name": "mcp_search",
            "input": {"text": args.get("text", "")},
        },
        ctx,
    )


async def _handle_mcp_install_server(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Forward to the ``mcp_install`` stdlib skill via invoke_skill.

    The skill owns the install lifecycle (registry fetch → permission
    gate → secret prompts → yaml write); the wrapper just shapes args.
    """
    from reyn.tools.invoke_skill import _handle as invoke_skill_handle

    return await invoke_skill_handle(
        {
            "name": "mcp_install",
            "input": {"text": args.get("text", "")},
        },
        ctx,
    )


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


__all__ = ["MCP_SEARCH_SERVER", "MCP_INSTALL_SERVER"]
