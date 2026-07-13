"""reyn_repo_list / reyn_repo_read ToolDefinitions — Wave 1 of M3 (ADR-0026).

Both capabilities are router-only dev-mode tools (gates.router="allow",
gates.phase="deny"). Phase doesn't need this; dev-debug is an
operator-side concern, not an agent-author concern.

The existing resolver in src/reyn/runtime/reyn_repo.py is preserved and
called directly from each handler (no OpContext shim needed — these
tools are pure filesystem reads with no workspace or events coupling).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import dev as _dev_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult


def _as_reyn_repo(result: dict) -> dict:
    """Tag a ``reyn.runtime.reyn_repo`` helper result with ``kind:"reyn_repo"`` so the offload seam
    (``core/offload/canonical.py``) routes it through the dedicated ``reyn_repo`` mapper — the file body
    / listing / match lines become the LLM-readable ``text`` — instead of the whole-dict ``structured``
    fallback that confused the agent in the FP-0056 dogfood incident (a doc read surfaced as a 600-char
    JSON-dict preview). The runtime helpers stay pure (no ``kind``); tagging lives at this tool seam,
    which is the only consumer of their results."""
    result["kind"] = "reyn_repo"
    return result

# router_tools.py derives its rendered description from this ToolDefinition
# via render_for_router() (registry lookup), not a separate literal — keep
# this the single source of truth for the LLM-facing text.
#
# The example path was "docs/en/concepts" until the docs i18n restructure
# (suffix-based: English lives at the repo-root path, Japanese is the same
# path with a ".ja.md" filename suffix — no "/en/" or "/ja/" directory
# prefix). That stale example reliably steered agents into guessing
# nonexistent "docs/en/..." paths (a tool-description-caused attractor, not
# generic LLM hallucination — confirmed by grep across dogfood journal
# findings hitting this exact wrong path repeatedly). Kept in sync with
# ``docs/concepts`` actually existing at the repo root.
# Relocated to reyn.tools.descriptions.dev (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_REYN_REPO_LIST_DESCRIPTION = _dev_descriptions.reyn_repo_list.text

# Parameters JSON schema must be byte-identical to the current
# router_tools.py ToolSpec.parameters for reyn_repo_list.
_REYN_REPO_LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}

# Description (= B22 schema-layer fix for affordance-bias attractor
# observed in batch 21). The previous text claimed "Use this for any
# 'how does Reyn / how does Reyn's X work?' question" which crowded out
# `recall` even when an indexed source covered the topic. The 4-part
# template (= what it does / when to use / when NOT to use / cross-
# reference) is per industry research (= Anthropic + OpenAI + LangChain
# + practitioner blogs).
#
# Constraints preserved (per A3 history audit):
#  - C1: file-read vs semantic-search distinction must be explicit
#  - C2: README curated navigation entry point retained as fallback
#  - web_search avoidance retained (= original HN first-touch motivation)
# Relocated to reyn.tools.descriptions.dev (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_REYN_REPO_READ_DESCRIPTION = _dev_descriptions.reyn_repo_read.text

# Parameters JSON schema for reyn_repo_read. Mirrors ``read_file`` /
# ``read_memory_body`` shape (= line-based ``offset`` / ``limit``) so the
# three "read one entry" surfaces are parameter-symmetric. When the
# slice args are provided, the 256-KB byte cap is bypassed — only the
# requested slice is materialised so a large file can be partially read.
_REYN_REPO_READ_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "offset": {
            "type": "integer",
            "description": _dev_descriptions.PARAMS["reyn_repo_read"]["offset"].text,
        },
        "limit": {
            "type": "integer",
            "description": _dev_descriptions.PARAMS["reyn_repo_read"]["limit"].text,
        },
    },
    "required": ["path"],
}


async def _handle_list(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_repo_list.

    Delegates to reyn.runtime.reyn_repo helpers which are the canonical
    implementation. No OpContext shim needed — these helpers are pure
    filesystem reads that don't access workspace or events.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.runtime.reyn_repo import (
        list_entries,
        resolve_reyn_root,
        safe_resolve_inside,
    )

    path = args.get("path", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return _as_reyn_repo({"error": str(exc)})
    try:
        target = safe_resolve_inside(root, path)
    except ValueError as exc:
        return _as_reyn_repo({"error": str(exc)})
    return _as_reyn_repo(list_entries(root, target, path))


async def _handle_read(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_repo_read.

    Delegates to reyn.runtime.reyn_repo helpers which are the canonical
    implementation. No OpContext shim needed — these helpers are pure
    filesystem reads that don't access workspace or events.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.runtime.reyn_repo import (
        read_text,
        resolve_reyn_root,
        safe_resolve_inside,
    )

    path = args.get("path", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return _as_reyn_repo({"error": str(exc)})
    try:
        target = safe_resolve_inside(root, path)
    except ValueError as exc:
        return _as_reyn_repo({"error": str(exc)})
    offset_raw = args.get("offset")
    limit_raw = args.get("limit")
    return _as_reyn_repo(read_text(
        target,
        path,
        offset=int(offset_raw) if offset_raw is not None else None,
        limit=int(limit_raw) if limit_raw is not None else None,
    ))


# Relocated to reyn.tools.descriptions.dev (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_REYN_REPO_GLOB_DESCRIPTION = _dev_descriptions.reyn_repo_glob.text

_REYN_REPO_GLOB_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": _dev_descriptions.PARAMS["reyn_repo_glob"]["pattern"].text,
        },
    },
    "required": ["pattern"],
}

# Relocated to reyn.tools.descriptions.dev (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_REYN_REPO_GREP_DESCRIPTION = _dev_descriptions.reyn_repo_grep.text

_REYN_REPO_GREP_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": _dev_descriptions.PARAMS["reyn_repo_grep"]["pattern"].text,
        },
        "path": {
            "type": "string",
            "description": _dev_descriptions.PARAMS["reyn_repo_grep"]["path"].text,
        },
        "glob": {
            "type": "string",
            "description": _dev_descriptions.PARAMS["reyn_repo_grep"]["glob"].text,
        },
        "case_sensitive": {
            "type": "boolean",
            "description": _dev_descriptions.PARAMS["reyn_repo_grep"]["case_sensitive"].text,
        },
        "max_results": {
            "type": "integer",
            "description": _dev_descriptions.PARAMS["reyn_repo_grep"]["max_results"].text,
        },
    },
    "required": ["pattern"],
}


async def _handle_glob(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_repo_glob."""
    from reyn.runtime.reyn_repo import glob_entries, resolve_reyn_root

    pattern = args.get("pattern", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return _as_reyn_repo({"error": str(exc)})
    return _as_reyn_repo(glob_entries(root, pattern))


async def _handle_grep(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_repo_grep."""
    from reyn.runtime.reyn_repo import grep_entries, resolve_reyn_root

    pattern = args.get("pattern", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return _as_reyn_repo({"error": str(exc)})
    return _as_reyn_repo(grep_entries(
        root,
        pattern=pattern,
        path=args.get("path", ""),
        glob=args.get("glob"),
        case_sensitive=bool(args.get("case_sensitive", False)),
        max_results=int(args.get("max_results", 50) or 50),
    ))


from reyn.core.offload.canonical import reyn_repo_to_canonical  # noqa: E402

REYN_REPO_LIST = ToolDefinition(
    canonical=reyn_repo_to_canonical,
    name="reyn_repo_list",
    router_dispatched=True,
    description=_REYN_REPO_LIST_DESCRIPTION,
    parameters=_REYN_REPO_LIST_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_list,
    purity="read_only",
    category="dev",
)

REYN_REPO_READ = ToolDefinition(
    canonical=reyn_repo_to_canonical,
    name="reyn_repo_read",
    router_dispatched=True,
    description=_REYN_REPO_READ_DESCRIPTION,
    parameters=_REYN_REPO_READ_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_read,
    purity="read_only",
    category="dev",
)

REYN_REPO_GLOB = ToolDefinition(
    canonical=reyn_repo_to_canonical,
    name="reyn_repo_glob",
    description=_REYN_REPO_GLOB_DESCRIPTION,
    parameters=_REYN_REPO_GLOB_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_glob,
    purity="read_only",
    category="dev",
)

REYN_REPO_GREP = ToolDefinition(
    canonical=reyn_repo_to_canonical,
    name="reyn_repo_grep",
    description=_REYN_REPO_GREP_DESCRIPTION,
    parameters=_REYN_REPO_GREP_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_grep,
    purity="read_only",
    category="dev",
)
