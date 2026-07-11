"""reyn_src_list / reyn_src_read ToolDefinitions — Wave 1 of M3 (ADR-0026).

Both capabilities are router-only dev-mode tools (gates.router="allow",
gates.phase="deny"). Phase doesn't need this; dev-debug is an
operator-side concern, not an agent-author concern.

The existing resolver in src/reyn/runtime/reyn_src.py is preserved and
called directly from each handler (no OpContext shim needed — these
tools are pure filesystem reads with no workspace or events coupling).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult


def _as_reyn_src(result: dict) -> dict:
    """Tag a ``reyn.runtime.reyn_src`` helper result with ``kind:"reyn_src"`` so the offload seam
    (``core/offload/canonical.py``) routes it through the dedicated ``reyn_src`` mapper — the file body
    / listing / match lines become the LLM-readable ``text`` — instead of the whole-dict ``structured``
    fallback that confused the agent in the FP-0056 dogfood incident (a doc read surfaced as a 600-char
    JSON-dict preview). The runtime helpers stay pure (no ``kind``); tagging lives at this tool seam,
    which is the only consumer of their results."""
    result["kind"] = "reyn_src"
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
_REYN_SRC_LIST_DESCRIPTION = (
    "List entries under a path inside Reyn's own repository "
    "(= the project that built this agent). Pass \"\" for "
    "the repo root. Returns names + types (file/dir). Use "
    "this to discover Reyn's source/doc layout before "
    "reading specific files. Examples: list \"\" for the "
    "top-level layout, \"docs/concepts\" for concept docs "
    "(English; Japanese translations are the same path with a "
    "\".ja.md\" filename suffix, not a separate directory), or "
    "any subdirectory path for its contents."
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
_REYN_SRC_READ_DESCRIPTION = (
    "Read a text file from Reyn's own repository by an exact "
    "repo-root-relative path. Use for: (a) reading a specific file the "
    "user named (e.g. README.md), or (b) navigating "
    "Reyn's source / docs when NO indexed source covers the topic. "
    "If an indexed source description mentions concepts / design / "
    "docs / Reyn, use `semantic_search` instead — guessing a file path is "
    "unreliable; semantic search over indexed chunks is not. Fallback "
    "entry point: reyn_src_read(\"README.md\") for the overview + "
    "curated map of deep-dive paths."
)

# Parameters JSON schema for reyn_src_read. Mirrors ``read_file`` /
# ``read_memory_body`` shape (= line-based ``offset`` / ``limit``) so the
# three "read one entry" surfaces are parameter-symmetric. When the
# slice args are provided, the 256-KB byte cap is bypassed — only the
# requested slice is materialised so a large file can be partially read.
_REYN_SRC_READ_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "offset": {
            "type": "integer",
            "description": (
                "Line number to start reading from (0-indexed). "
                "Omit to start at the beginning of the file. When set "
                "(with or without limit), the 256-KB byte cap is "
                "bypassed by line-streaming only the requested slice."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                "Number of lines to read from `offset`. "
                "Omit to read through end of file."
            ),
        },
    },
    "required": ["path"],
}


async def _handle_list(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_src_list.

    Delegates to reyn.runtime.reyn_src helpers which are the canonical
    implementation. No OpContext shim needed — these helpers are pure
    filesystem reads that don't access workspace or events.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.runtime.reyn_src import (
        list_entries,
        resolve_reyn_root,
        safe_resolve_inside,
    )

    path = args.get("path", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return _as_reyn_src({"error": str(exc)})
    try:
        target = safe_resolve_inside(root, path)
    except ValueError as exc:
        return _as_reyn_src({"error": str(exc)})
    return _as_reyn_src(list_entries(root, target, path))


async def _handle_read(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_src_read.

    Delegates to reyn.runtime.reyn_src helpers which are the canonical
    implementation. No OpContext shim needed — these helpers are pure
    filesystem reads that don't access workspace or events.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.runtime.reyn_src import (
        read_text,
        resolve_reyn_root,
        safe_resolve_inside,
    )

    path = args.get("path", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return _as_reyn_src({"error": str(exc)})
    try:
        target = safe_resolve_inside(root, path)
    except ValueError as exc:
        return _as_reyn_src({"error": str(exc)})
    offset_raw = args.get("offset")
    limit_raw = args.get("limit")
    return _as_reyn_src(read_text(
        target,
        path,
        offset=int(offset_raw) if offset_raw is not None else None,
        limit=int(limit_raw) if limit_raw is not None else None,
    ))


_REYN_SRC_GLOB_DESCRIPTION = (
    "Find files in Reyn's own repository by glob pattern (e.g. "
    "'docs/**/*.md', 'src/**/router*.py'). Returns up to 200 "
    "repo-root-relative paths, alphabetically sorted. Use this when "
    "you need to enumerate files matching a structural pattern; for "
    "content search use reyn_src_grep, for a single named file use "
    "reyn_src_read."
)

_REYN_SRC_GLOB_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern (e.g. '**/*.py', 'docs/**/*.md').",
        },
    },
    "required": ["pattern"],
}

_REYN_SRC_GREP_DESCRIPTION = (
    "Search file contents in Reyn's own repository by regex. Returns "
    "up to 50 matches as {path, line, snippet}. `path` scopes the "
    "search (default = whole repo); `glob` further narrows by filename "
    "(e.g. '**/*.py'). Use this for 'where in the Reyn source is X "
    "handled' style questions; for structural enumeration use "
    "reyn_src_glob, for reading one known file use reyn_src_read."
)

_REYN_SRC_GREP_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Regex pattern (Python `re` syntax).",
        },
        "path": {
            "type": "string",
            "description": (
                "Repo-relative directory or file to scope the search. "
                "Default = repo root. Use '' for repo root."
            ),
        },
        "glob": {
            "type": "string",
            "description": (
                "Optional filename glob filter (e.g. '**/*.py'). "
                "When omitted, all text files under `path` are searched."
            ),
        },
        "case_sensitive": {
            "type": "boolean",
            "description": "Default false (= case-insensitive).",
        },
        "max_results": {
            "type": "integer",
            "description": "Cap on match count. Default 50.",
        },
    },
    "required": ["pattern"],
}


async def _handle_glob(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_src_glob."""
    from reyn.runtime.reyn_src import glob_entries, resolve_reyn_root

    pattern = args.get("pattern", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return _as_reyn_src({"error": str(exc)})
    return _as_reyn_src(glob_entries(root, pattern))


async def _handle_grep(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Handler for reyn_src_grep."""
    from reyn.runtime.reyn_src import grep_entries, resolve_reyn_root

    pattern = args.get("pattern", "")
    try:
        root = resolve_reyn_root()
    except RuntimeError as exc:
        return _as_reyn_src({"error": str(exc)})
    return _as_reyn_src(grep_entries(
        root,
        pattern=pattern,
        path=args.get("path", ""),
        glob=args.get("glob"),
        case_sensitive=bool(args.get("case_sensitive", False)),
        max_results=int(args.get("max_results", 50) or 50),
    ))


from reyn.core.offload.canonical import reyn_src_to_canonical  # noqa: E402

REYN_SRC_LIST = ToolDefinition(
    canonical=reyn_src_to_canonical,
    name="reyn_src_list",
    router_dispatched=True,
    description=_REYN_SRC_LIST_DESCRIPTION,
    parameters=_REYN_SRC_LIST_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_list,
    purity="read_only",
    category="dev",
)

REYN_SRC_READ = ToolDefinition(
    canonical=reyn_src_to_canonical,
    name="reyn_src_read",
    router_dispatched=True,
    description=_REYN_SRC_READ_DESCRIPTION,
    parameters=_REYN_SRC_READ_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_read,
    purity="read_only",
    category="dev",
)

REYN_SRC_GLOB = ToolDefinition(
    canonical=reyn_src_to_canonical,
    name="reyn_src_glob",
    description=_REYN_SRC_GLOB_DESCRIPTION,
    parameters=_REYN_SRC_GLOB_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_glob,
    purity="read_only",
    category="dev",
)

REYN_SRC_GREP = ToolDefinition(
    canonical=reyn_src_to_canonical,
    name="reyn_src_grep",
    description=_REYN_SRC_GREP_DESCRIPTION,
    parameters=_REYN_SRC_GREP_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_grep,
    purity="read_only",
    category="dev",
)
