"""MCP verb-object handlers — three-axis install split.

Router-callable MCP verbs under the single ``mcp`` category. The install
surface is split along the **source axis** (= where the server comes from)
so each verb has a structurally narrow input and no XOR ambiguity:

  - ``mcp__search_registry``  — search the official MCP registry
  - ``mcp__install_registry`` — install from the official MCP registry
                                (paired with ``mcp__search_registry``)
  - ``mcp__install_package``  — install from a third-party package channel
                                (npm / pypi / docker / github URL)
  - ``mcp__install_local``    — install a local command (LLM-authored
                                script, dev server) by writing a
                                ``{command, args}`` entry directly
  - ``mcp__list_servers``     — list installed servers
                                (existing ``LIST_MCP_SERVERS``)
  - ``mcp__list_tools``       — list a server's tools as
                                ``<server>__<tool>`` identifiers
                                (existing ``LIST_MCP_TOOLS``)
  - ``mcp__call_tool``        — call a tool by ``<server>__<tool>``
                                identifier (this module)
  - ``mcp__drop_server``      — remove an installed server
                                (existing ``MCP_DROP_SERVER``)

No skills are spawned. All install paths converge on the same
``.reyn/mcp.yaml`` entry shape via op_runtime helpers; the verbs differ
only in how they obtain the package metadata before that write.

Secret handling for the two registry-aware verbs
(``mcp__install_registry``, ``mcp__install_package``) is **strict args
+ guide**: when package metadata declares ``isSecret: true`` env-vars
and the operator has not pre-supplied them (via ``env_overrides`` or
``reyn secret set``), the install short-circuits with
``status="needs_secrets"`` + a guide. ``mcp__install_local`` cannot
auto-detect secrets (= operator supplies ``env_overrides`` inline if
needed).
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from reyn.tools.mcp import _MCP_TOOL_ARGS_KEY  # #1646: single-source the inner-args key
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── mcp__search_registry ──────────────────────────────────────────────────────


_MCP_SEARCH_REGISTRY_DESCRIPTION = (
    "Search the official MCP registry for servers matching a "
    "natural-language capability request. Returns candidates whose "
    "'name' field feeds mcp__install_registry. Multilingual — accepts "
    "queries in any language."
)

_MCP_SEARCH_REGISTRY_PARAMETERS: dict[str, Any] = {
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


async def _handle_mcp_search_registry(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Registry search — uses the same RegistryClient backend as the CLI
    ``reyn mcp search`` so chat and CLI surfaces stay in lock-step.

    Passes the user's text verbatim to the registry; the registry's own
    indexing handles multilingual matching. The pre-#882 ``mcp_search``
    skill applied a Japanese→English keyword extraction preprocessor
    (= `_extract_keyword`); that workaround is dropped now that the
    handler runs in the router context with full async HTTP available.
    """
    text = str(args.get("text", "") or "").strip()
    if not text:
        return {
            "status": "error",
            "data": {"error": "text is required"},
        }

    from reyn.core.registry.client import RegistryClient, RegistryError

    try:
        async with RegistryClient() as client:
            candidates = await client.search(text, limit=20)
    except RegistryError as exc:
        return {
            "status": "error",
            "data": {
                "query": text,
                "candidates": [],
                "error": str(exc),
            },
        }

    return {
        "status": "ok",
        "data": {
            "query": text,
            "candidates": [asdict(c) for c in candidates],
        },
    }


# ── mcp__install_registry ─────────────────────────────────────────────────────


_MCP_INSTALL_REGISTRY_DESCRIPTION = (
    "Install an MCP server from the official MCP registry by its "
    "registry name (server_id from mcp__search_registry candidates[].name). "
    "When the server requires secret environment variables that the "
    "operator has not yet set, the call returns status='needs_secrets' "
    "with a guide explaining the `reyn secret set <KEY>` command; relay "
    "that to the user and retry after they confirm secrets are set."
)

_MCP_INSTALL_REGISTRY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server_id": {
            "type": "string",
            "description": (
                "Registry identifier from mcp__search_registry "
                "(= candidates[].name, "
                "e.g. 'io.github.modelcontextprotocol/server-time')."
            ),
        },
        "env_overrides": {
            "type": "object",
            "description": (
                "Inline env values. Usually NOT needed — the first call "
                "returns status='needs_secrets' listing which keys to "
                "set via `reyn secret set <KEY>`; only pass this dict "
                "when the operator supplied values inline."
            ),
            "additionalProperties": {"type": "string"},
        },
    },
    "required": ["server_id"],
}


async def _handle_mcp_install_registry(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Install from the official MCP registry by server_id only.

    Strict input: server_id is required, non-registry installs go through
    mcp__install_package or mcp__install_local instead. Secret handling
    matches the registry-aware contract — see module docstring.
    """
    from reyn.core.op_runtime.mcp_install import handle as mcp_install_handle
    from reyn.schemas.models import MCPInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.tools.op_context_bridge import build_legacy_op_context

    server_id = str(args.get("server_id") or "")
    if not server_id:
        return {
            "status": "error",
            "data": {
                "error": (
                    "server_id is required. Call mcp__search_registry "
                    "first to find a candidate, or use "
                    "mcp__install_package / mcp__install_local for "
                    "non-registry installs."
                ),
            },
        }

    try:
        op = MCPInstallIROp(
            kind="mcp_install",
            server_id=server_id,
            scope="local",
            env_overrides=args.get("env_overrides"),
            source=None,
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

    # #1442 follow-up: get the OpContext from the SINGLE-SOURCE bridge (the same
    # one file/compact/recall/web_fetch/sandboxed_exec use), not a hand-built one.
    # On the chat-router path this yields the real Workspace rooted at the agent's
    # workspace_base_dir; hand-building from ctx.workspace (None there) wrote the
    # config to cwd. Only the install-specific decl + skill_name are overridden.
    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.skill_name = "mcp__install_registry"

    result = await mcp_install_handle(op, op_ctx, caller="control_ir")
    return {"status": "ok", "data": result}


# ── mcp__install_package ──────────────────────────────────────────────────────


_MCP_INSTALL_PACKAGE_DESCRIPTION = (
    "Install an MCP server from a third-party package channel "
    "(npm / pypi / docker) or a GitHub repo URL. Use when the server "
    "isn't in the official registry (= mcp__search_registry returned "
    "no match). Secret detection works the same as install_registry "
    "for npm/pypi/docker; github URLs cannot pre-declare secrets."
)

_MCP_INSTALL_PACKAGE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["npm", "pypi", "docker", "github"],
            "description": "Package channel.",
        },
        "identifier": {
            "type": "string",
            "description": (
                "npm: package name (e.g. '@scope/server-foo')\n"
                "pypi: distribution name (e.g. 'my-mcp-server')\n"
                "docker: image with optional tag "
                "(e.g. 'org/img:v1')\n"
                "github: full URL "
                "(e.g. 'https://github.com/owner/repo' or "
                "'https://github.com/owner/repo/tree/<ref>/src/<sub>')"
            ),
        },
        "version": {
            "type": "string",
            "description": (
                "Version constraint. npm/pypi/docker only — "
                "ignored for github."
            ),
        },
        "env_overrides": {
            "type": "object",
            "description": (
                "Inline env values when the operator provides them; "
                "otherwise expect status='needs_secrets' on the "
                "first call (npm/pypi/docker only)."
            ),
            "additionalProperties": {"type": "string"},
        },
    },
    "required": ["kind", "identifier"],
}


def _build_source_string(kind: str, identifier: str, version: str) -> str:
    """Compose the source_resolver inline string from structured fields."""
    if kind == "github":
        return identifier  # URL is the specifier itself
    if kind == "npm":
        return f"npm:{identifier}@{version}" if version else f"npm:{identifier}"
    if kind == "pypi":
        return f"pypi:{identifier}=={version}" if version else f"pypi:{identifier}"
    if kind == "docker":
        return f"docker:{identifier}:{version}" if version else f"docker:{identifier}"
    return identifier  # pragma: no cover — enum constrains this


async def _handle_mcp_install_package(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Install via a structured package specifier.

    Composes an inline ``source`` string the source_resolver understands,
    then delegates to op_runtime/mcp_install with ``server_id=""`` so the
    registry HTTP path is skipped.
    """
    from reyn.core.op_runtime.mcp_install import handle as mcp_install_handle
    from reyn.schemas.models import MCPInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.tools.op_context_bridge import build_legacy_op_context

    kind = str(args.get("kind") or "")
    identifier = str(args.get("identifier") or "")
    version = str(args.get("version") or "")
    if kind not in {"npm", "pypi", "docker", "github"}:
        return {
            "status": "error",
            "data": {
                "error": (
                    f"kind must be one of npm/pypi/docker/github; "
                    f"got {kind!r}"
                ),
            },
        }
    if not identifier:
        return {
            "status": "error",
            "data": {"error": "identifier is required"},
        }

    source = _build_source_string(kind, identifier, version)

    try:
        op = MCPInstallIROp(
            kind="mcp_install",
            server_id="",
            scope="local",
            env_overrides=args.get("env_overrides"),
            source=source,
            extra_args=None,
        )
    except Exception as exc:
        return {
            "status": "error",
            "data": {"error": f"invalid args: {exc}"},
        }

    decl = PermissionDecl()
    decl.file_write = [{"path": ".reyn/mcp.yaml"}]
    decl.secret_write = ["*"]

    # #1442 follow-up: single-source bridge (see _handle_mcp_install_registry) —
    # real Workspace on the chat path; override only the install-specific fields.
    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.skill_name = "mcp__install_package"

    result = await mcp_install_handle(op, op_ctx, caller="control_ir")
    return {"status": "ok", "data": result}


# ── mcp__install_local ────────────────────────────────────────────────────────


_MCP_INSTALL_LOCAL_DESCRIPTION = (
    "Install a local MCP server by registering a {command, args} pair "
    "directly. Use for LLM-authored scripts or local development "
    "servers. Bypasses package registries — cannot auto-detect required "
    "secrets, so pass env_overrides inline when the server needs env-vars."
)

_MCP_INSTALL_LOCAL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Short config key written under mcp.servers.<name> "
                "(e.g. 'weather'). Used as the server prefix in "
                "mcp__call_tool's '<server>__<tool>' identifier."
            ),
        },
        "command": {
            "type": "string",
            "description": (
                "Executable to spawn (e.g. 'python', 'node', 'uvx', "
                "or an absolute path)."
            ),
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Command-line arguments. Typically the script path "
                "(e.g. ['/tmp/weather_mcp.py']) plus flags the server "
                "expects."
            ),
        },
        "env_overrides": {
            "type": "object",
            "description": "Inline env values for the spawned process.",
            "additionalProperties": {"type": "string"},
        },
    },
    "required": ["name", "command", "args"],
}


async def _handle_mcp_install_local(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Register a local MCP server entry by writing .reyn/mcp.yaml directly.

    Bypasses registry + source_resolver. The verb's input maps 1:1 to
    the loader's stdio-server config shape (``{type, command, args, env}``)
    so the MCPClient launcher can spawn the process without further
    metadata.
    """
    from pathlib import Path

    from reyn.core.op_runtime.mcp_install import (
        _read_yaml_config,
        _scope_to_path,
        _write_yaml_config,
    )
    from reyn.security.permissions.permissions import PermissionDecl

    name = str(args.get("name") or "").strip()
    command = str(args.get("command") or "").strip()
    raw_args = args.get("args")
    if not name:
        return {"status": "error", "data": {"error": "name is required"}}
    if not command:
        return {"status": "error", "data": {"error": "command is required"}}
    if not isinstance(raw_args, list):
        return {
            "status": "error",
            "data": {"error": "args must be a list of strings"},
        }
    cmd_args = [str(a) for a in raw_args]
    env_overrides = args.get("env_overrides") or {}
    if not isinstance(env_overrides, Mapping):
        return {
            "status": "error",
            "data": {"error": "env_overrides must be an object"},
        }

    project_root = Path.cwd()
    rs = getattr(ctx, "router_state", None)
    workspace = getattr(ctx, "workspace", None)
    if workspace is not None and hasattr(workspace, "root"):
        project_root = Path(workspace.root)
    config_path = _scope_to_path("local", project_root)

    decl = PermissionDecl()
    decl.file_write = [{"path": ".reyn/mcp.yaml"}]
    resolver = getattr(rs, "permission_resolver", None) if rs is not None else None
    if resolver is not None:
        await resolver.require_file_write(decl, str(config_path), "mcp__install_local")

    entry: dict[str, Any] = {
        "type": "stdio",
        "command": command,
        "args": cmd_args,
    }
    if env_overrides:
        entry["env"] = {str(k): str(v) for k, v in env_overrides.items()}

    data = _read_yaml_config(config_path)
    servers = data.setdefault("mcp", {}).setdefault("servers", {})
    servers[name] = entry
    _write_yaml_config(config_path, data)

    return {
        "status": "ok",
        "data": {
            "kind": "mcp_install_local",
            "name": name,
            "config_path": str(config_path),
            "entry": entry,
        },
    }


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
        # #1646: the target tool's params nest under "tool_args", NOT "args" — the
        # universal-scheme live path is invoke_action(action_name="mcp__call_tool",
        # args={tool, tool_args:{...}}); a nested "args" here collided with
        # invoke_action's own "args" (the LLM collapsed it → empty at the MCP call).
        _MCP_TOOL_ARGS_KEY: {
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
            # #1646: read the LLM's params from tool_args (K3 collision-kill) AND pass
            # them to mcp.py under the SAME key it now reads (K4 — pre-fix this passed
            # "args" while mcp.py read tool_args → delegation dropped to {}).
            _MCP_TOOL_ARGS_KEY: dict(args.get(_MCP_TOOL_ARGS_KEY) or {}),
        },
        ctx,
    )


# ── ToolDefinitions ──────────────────────────────────────────────────────────


MCP_SEARCH_REGISTRY = ToolDefinition(
    name="mcp_search_registry",
    description=_MCP_SEARCH_REGISTRY_DESCRIPTION,
    parameters=_MCP_SEARCH_REGISTRY_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_mcp_search_registry,
    category="discovery",
    purity="read_only",
)


MCP_INSTALL_REGISTRY = ToolDefinition(
    name="mcp_install_registry",
    description=_MCP_INSTALL_REGISTRY_DESCRIPTION,
    parameters=_MCP_INSTALL_REGISTRY_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_mcp_install_registry,
    category="io",
    purity="side_effect",
)


MCP_INSTALL_PACKAGE = ToolDefinition(
    name="mcp_install_package",
    description=_MCP_INSTALL_PACKAGE_DESCRIPTION,
    parameters=_MCP_INSTALL_PACKAGE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_mcp_install_package,
    category="io",
    purity="side_effect",
)


MCP_INSTALL_LOCAL = ToolDefinition(
    name="mcp_install_local",
    description=_MCP_INSTALL_LOCAL_DESCRIPTION,
    parameters=_MCP_INSTALL_LOCAL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_mcp_install_local,
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


__all__ = [
    "MCP_SEARCH_REGISTRY",
    "MCP_INSTALL_REGISTRY",
    "MCP_INSTALL_PACKAGE",
    "MCP_INSTALL_LOCAL",
    "MCP_CALL_TOOL",
]
