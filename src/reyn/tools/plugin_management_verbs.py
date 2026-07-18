"""Plugin verb-object handlers — plugin_install / plugin_uninstall (ADR 0064 P2).

Router-callable plugin management verbs, mirroring
``pipeline_management_verbs.py`` / ``skill_verbs.py`` as closely as possible:
a thin ``ToolContext``-facing wrapper that builds the typed op
(``PluginInstallIROp`` / ``PluginUninstallIROp``), a ``PermissionDecl``
declaring the capability surfaces this op needs (§3.10), and delegates to
``op_runtime/plugin_install.py`` / ``op_runtime/plugin_uninstall.py`` via the
``build_legacy_op_context`` bridge (same pattern every other install verb
uses).

**Security-critical**: ``plugin_install``'s permission surface is a
COMPOSITE of existing gates (§3.10, no new bool axis — the #571 collapse arc
removed those): ``file.write`` (recursive, scoped to ``~/.reyn/plugins/`` —
the global-copy write is OUTSIDE the default ``.reyn/`` write zone, so the
EXISTING ``require_file_write`` JIT-ask/deny path already covers it) +
``http.get`` (wildcard — covers BOTH the ``{kind:"git"}`` remote-code fetch
AND the install-time dependency-materialisation fetch; the op handler gates
each SPECIFIC host as it becomes known, mirroring ``pipeline_install``'s
source-fetch gate).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import plugin_management as _plugin_management_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── plugin_install ─────────────────────────────────────────────────────────

_PLUGIN_INSTALL_DESCRIPTION = _plugin_management_descriptions.plugin_install.text

_PLUGIN_SOURCE_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "builtin"},
                "name": {"type": "string", "description": "reyn's own shipped plugin name."},
            },
            "required": ["kind", "name"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "local"},
                "path": {"type": "string", "description": "Local plugin directory you authored/tested."},
            },
            "required": ["kind", "path"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "git"},
                "url": {"type": "string", "description": "Remote git URL (highest trust risk)."},
            },
            "required": ["kind", "url"],
            "additionalProperties": False,
        },
    ],
    "description": (
        _plugin_management_descriptions.PARAMS["plugin_management__install"]["source"].text
    ),
}

_PLUGIN_INSTALL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": _PLUGIN_SOURCE_SCHEMA,
        "name": {
            "type": "string",
            "description": (
                _plugin_management_descriptions.PARAMS["plugin_management__install"]["name"].text
            ),
        },
    },
    "required": ["source"],
}


async def _handle_plugin_install(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Install/promote a plugin. Delegates to op_runtime/plugin_install.handle
    via build_legacy_op_context (same bridge pattern as every other install
    verb)."""
    from pathlib import Path

    from reyn.core.op_runtime.plugin_install import handle as plugin_install_handle
    from reyn.core.op_runtime.plugin_install import plugins_root
    from reyn.schemas.models import PluginInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.tools.op_context_bridge import build_legacy_op_context

    raw_source = args.get("source")
    if not isinstance(raw_source, Mapping):
        return {"status": "error", "data": {"error": "source is required (a {kind, ...} object)"}}

    raw_name = args.get("name")
    name_override = str(raw_name).strip() if raw_name else None

    try:
        op = PluginInstallIROp(
            kind="plugin_install",
            source=dict(raw_source),
            name=name_override,
        )
    except Exception as exc:
        return {"status": "error", "data": {"error": f"invalid args: {exc}"}}

    decl = PermissionDecl()
    # Recursive scope: the final install directory name is not known until
    # the manifest is resolved (kind="builtin"/"local" pass a name/path, not
    # the plugin's own manifest-declared name) — the declared authority
    # covers the whole global-copy root, matching how the op itself gates
    # the SPECIFIC resolved path via require_file_write at handler time.
    decl.file_write = [{"path": str(plugins_root()), "scope": "recursive"}]
    # Wildcard: covers both the {kind:"git"} remote-code fetch AND the
    # install-time dependency-materialisation fetch (pypi.org) — the op
    # handler gates each SPECIFIC host as it becomes known (mirrors
    # pipeline_install's source-fetch gate).
    decl.http_get = [{"host": "*"}]

    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.actor = "plugin_management__install"

    result = await plugin_install_handle(op, op_ctx)
    return {"status": "ok", "data": result}


# ── plugin_uninstall ────────────────────────────────────────────────────────

_PLUGIN_UNINSTALL_DESCRIPTION = _plugin_management_descriptions.plugin_uninstall.text

_PLUGIN_UNINSTALL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                _plugin_management_descriptions.PARAMS["plugin_management__uninstall"]["name"].text
            ),
        },
    },
    "required": ["name"],
}


async def _handle_plugin_uninstall(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Uninstall a plugin. Delegates to op_runtime/plugin_uninstall.handle via
    build_legacy_op_context (same bridge pattern as plugin_install)."""
    from reyn.core.op_runtime.plugin_install import plugins_root
    from reyn.core.op_runtime.plugin_uninstall import handle as plugin_uninstall_handle
    from reyn.schemas.models import PluginUninstallIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.tools.op_context_bridge import build_legacy_op_context

    name = str(args.get("name") or "").strip()
    if not name:
        return {"status": "error", "data": {"error": "name is required"}}

    try:
        op = PluginUninstallIROp(kind="plugin_uninstall", name=name)
    except Exception as exc:
        return {"status": "error", "data": {"error": f"invalid args: {exc}"}}

    decl = PermissionDecl()
    decl.file_write = [
        {"path": str(plugins_root()), "scope": "recursive"},
        {"path": ".reyn/config/mcp.yaml", "scope": "just_path"},
        {"path": ".reyn/config/pipelines.yaml", "scope": "just_path"},
        {"path": ".reyn/config/skills.yaml", "scope": "just_path"},
    ]

    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.actor = "plugin_management__uninstall"

    result = await plugin_uninstall_handle(op, op_ctx)
    return {"status": "ok", "data": result}


# ── ToolDefinitions ───────────────────────────────────────────────────────────

from reyn.core.offload.canonical import (  # noqa: E402
    plugin_install_verb_to_canonical,
    plugin_uninstall_verb_to_canonical,
)

_PLUGIN_DOC_REF = "docs/deep-dives/proposals/0064-plugin-model.md"

PLUGIN_INSTALL = ToolDefinition(
    canonical=plugin_install_verb_to_canonical,
    # Named distinctly from the "plugin_install" OP KIND (op_runtime/plugin_install.py,
    # the phase-level Control IR surface — a pipeline step can also target
    # kind="plugin_install" directly) — a shared name would collide at
    # declare_canonical (two different mappers claiming one source_id).
    # Mirrors the "mcp_install_local" vs "mcp_install" op-kind precedent.
    name="plugin_management__install",
    description=_PLUGIN_INSTALL_DESCRIPTION,
    parameters=_PLUGIN_INSTALL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_plugin_install,
    category="io",
    purity="side_effect",
    doc_ref=_PLUGIN_DOC_REF,
)

PLUGIN_UNINSTALL = ToolDefinition(
    canonical=plugin_uninstall_verb_to_canonical,
    # Mirrors PLUGIN_INSTALL's naming rationale above (distinct from the
    # "plugin_uninstall" op kind).
    name="plugin_management__uninstall",
    description=_PLUGIN_UNINSTALL_DESCRIPTION,
    parameters=_PLUGIN_UNINSTALL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_plugin_uninstall,
    category="io",
    purity="side_effect",
    doc_ref=_PLUGIN_DOC_REF,
)

__all__ = ["PLUGIN_INSTALL", "PLUGIN_UNINSTALL"]
