"""reyn_src_list / reyn_src_read ToolDefinitions — Wave 1 of M3 (ADR-0026).

Both capabilities are router-only dev-mode tools (gates.router="allow",
gates.phase="deny"). Phase doesn't need this; dev-debug is an
operator-side concern, not a skill-author concern.

The existing resolver in src/reyn/chat/reyn_src.py is preserved and
called directly from each handler (no OpContext shim needed — these
tools are pure filesystem reads with no workspace or events coupling).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Description must be byte-identical to the current router_tools.py
# ToolSpec.description for reyn_src_list (lines 776-783). Copied verbatim.
_REYN_SRC_LIST_DESCRIPTION = (
    "List entries under a path inside Reyn's own repository "
    "(= the project that built this agent). Pass \"\" for "
    "the repo root. Returns names + types (file/dir). Use "
    "this to discover Reyn's source/doc layout before "
    "reading specific files. Examples: list \"\" for the "
    "top-level layout, \"docs/en/concepts\" for concept "
    "docs, \"src/reyn/chat\" for the chat layer source."
)

# Parameters JSON schema must be byte-identical to the current
# router_tools.py ToolSpec.parameters for reyn_src_list.
_REYN_SRC_LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}

# Description must be byte-identical to the current router_tools.py
# ToolSpec.description for reyn_src_read (lines 798-805). Copied verbatim.
_REYN_SRC_READ_DESCRIPTION = (
    "Read a text file from Reyn's own repository. Path is "
    "repo-root-relative (= same paths the user sees on "
    "GitHub). Start with reyn_src_read(\"README.md\") for "
    "an overview and a curated index of deep-dive paths. "
    "Use this for any \"how does Reyn / how does Reyn's X "
    "work?\" question — Reyn's source is the authoritative "
    "answer, not web search."
)

# Parameters JSON schema must be byte-identical to the current
# router_tools.py ToolSpec.parameters for reyn_src_read.
_REYN_SRC_READ_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}


async def _handle_list(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_src_list.

    Delegates to reyn.chat.reyn_src helpers which are the canonical
    implementation. No OpContext shim needed — these helpers are pure
    filesystem reads that don't access workspace or events.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.chat.reyn_src import (
        list_entries,
        resolve_reyn_root,
        safe_resolve_inside,
    )

    path = args.get("path", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return {"error": str(exc)}
    try:
        target = safe_resolve_inside(root, path)
    except ValueError as exc:
        return {"error": str(exc)}
    return list_entries(root, target, path)


async def _handle_read(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_src_read.

    Delegates to reyn.chat.reyn_src helpers which are the canonical
    implementation. No OpContext shim needed — these helpers are pure
    filesystem reads that don't access workspace or events.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.chat.reyn_src import (
        read_text,
        resolve_reyn_root,
        safe_resolve_inside,
    )

    path = args.get("path", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return {"error": str(exc)}
    try:
        target = safe_resolve_inside(root, path)
    except ValueError as exc:
        return {"error": str(exc)}
    return read_text(target, path)


REYN_SRC_LIST = ToolDefinition(
    name="reyn_src_list",
    description=_REYN_SRC_LIST_DESCRIPTION,
    parameters=_REYN_SRC_LIST_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_list,
    purity="read_only",
    category="dev",
)

REYN_SRC_READ = ToolDefinition(
    name="reyn_src_read",
    description=_REYN_SRC_READ_DESCRIPTION,
    parameters=_REYN_SRC_READ_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_read,
    purity="read_only",
    category="dev",
)
