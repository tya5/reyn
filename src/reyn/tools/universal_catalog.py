"""Universal catalog wrappers — FP-0034 Phase 1 foundation.

This module defines the 4 universal wrapper ToolDefinitions
(``list_actions`` / ``search_actions`` / ``describe_action`` /
``invoke_action``) plus the qualified-name parser/builder and the
canonical 13-category enum that FP-0034 establishes.

Per FP-0034 §D1, the universal catalog replaces the per-category
discover ops (= ``list_skills`` / ``list_mcp_tools`` / ``list_memory``
etc.) with 4 wrappers that cover all 13 categories uniformly. Per
§D18, qualified names use ``<category>__<entry_name>`` format with
``__`` (double underscore) as the separator. Inside ``entry_name``
arbitrary characters (including ``.``) are allowed, so MCP tools
like ``mcp.tool__brave.search`` round-trip correctly.

PR-1 scope (this file):
  - Category enum constants + helper predicates
  - Qualified-name parse / build / validate
  - 4 ToolDefinitions with production-ready schemas
  - Stub handlers that raise NotImplementedError until PR-2 lands the
    dispatcher (= ``universal_dispatch.py``)
  - D14 visibility-gating helpers (= ``is_search_available`` /
    ``is_exec_available``); the actual schema-level gating happens
    at the integration layer (= router_tools.py in PR-3)

PR-2 (next): qualified-name → handler routing across all 13 categories,
single-source ``rag.corpus`` curry (= D19 resource invoke), error-with-
suggestions response (= D12).

PR-3 (later): router integration — tools= placement, SP refactor
(category-only description), ``__init__.py`` registration.

PR-4 (later): new op ``mcp.operation__drop_server`` for the destructor
side of MCP server CRUD (D23).

PR-5 (later): Tier 3 LLMReplay fixtures + e2e verification of §Phase 1
verification 1-9.
"""
from __future__ import annotations

from typing import Any, Final, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult


# ── Canonical 13-category enum (FP-0034 §D18 master taxonomy) ──────────────
#
# Order matches the master table in FP-0034 §D18 so reviewers reading the
# design doc and the code see the same shape. ``exec`` ships last because
# it is the only category with hard sandbox-backend gating (= D14 / D14-ext).
CATEGORIES: Final[tuple[str, ...]] = (
    "skill",
    "agent.peer",
    "mcp.server",
    "mcp.tool",
    "mcp.operation",
    "file",
    "web",
    "memory.entry",
    "memory.operation",
    "reyn.source",
    "rag.corpus",
    "rag.operation",
    "exec",
)


# The qualified-name separator. Double-underscore is chosen so dotted
# categories (``mcp.tool``) and dotted entry names (``brave.search``)
# never collide with the boundary; see FP-0034 §D18.
_NAME_SEPARATOR: Final[str] = "__"


# ── Qualified name parse / build / validate ────────────────────────────────


def split_qualified_name(qualified_name: str) -> tuple[str, str]:
    """Split a qualified name into (category, entry_name).

    Splits on the FIRST occurrence of ``__`` (double underscore). The
    category portion must match one of CATEGORIES; otherwise raises
    ValueError. The entry name may contain any characters including
    further ``__`` sequences (which stay inside the entry portion).

    Examples:
        ``skill__code_review``       → ("skill", "code_review")
        ``mcp.tool__brave.search``   → ("mcp.tool", "brave.search")
        ``mcp.operation__drop_server`` → ("mcp.operation", "drop_server")
        ``rag.corpus__meetings``     → ("rag.corpus", "meetings")

    Raises:
        ValueError: when the input has no ``__`` separator, the category
            portion is not in CATEGORIES, or the entry_name is empty.
    """
    if not isinstance(qualified_name, str):
        raise ValueError(
            f"qualified_name must be str, got {type(qualified_name).__name__}"
        )
    sep_idx = qualified_name.find(_NAME_SEPARATOR)
    if sep_idx < 0:
        raise ValueError(
            f"qualified_name {qualified_name!r} missing {_NAME_SEPARATOR!r} "
            f"separator; expected <category>__<entry_name>"
        )
    category = qualified_name[:sep_idx]
    entry_name = qualified_name[sep_idx + len(_NAME_SEPARATOR):]
    if category not in CATEGORIES:
        raise ValueError(
            f"qualified_name {qualified_name!r} has unknown category "
            f"{category!r}; expected one of {list(CATEGORIES)}"
        )
    if not entry_name:
        raise ValueError(
            f"qualified_name {qualified_name!r} has empty entry_name"
        )
    return category, entry_name


def build_qualified_name(category: str, entry_name: str) -> str:
    """Build a qualified name from category + entry_name.

    Validates ``category`` against CATEGORIES and rejects empty
    ``entry_name``. Inverse of split_qualified_name (round-trips).
    """
    if category not in CATEGORIES:
        raise ValueError(
            f"unknown category {category!r}; expected one of {list(CATEGORIES)}"
        )
    if not entry_name:
        raise ValueError("entry_name must be non-empty")
    return f"{category}{_NAME_SEPARATOR}{entry_name}"


def is_valid_qualified_name(qualified_name: str) -> bool:
    """Return True iff ``qualified_name`` parses cleanly.

    Convenience predicate; identical semantics to wrapping
    split_qualified_name in a try/except ValueError. Useful in
    list/filter pipelines and schema validators.
    """
    try:
        split_qualified_name(qualified_name)
    except ValueError:
        return False
    return True


# ── D14 visibility gating helpers ──────────────────────────────────────────


def is_search_available(*, action_retrieval_embedding_class: str | None) -> bool:
    """Return True iff ``search_actions`` should be exposed to the LLM.

    Per FP-0034 §D14, ``search_actions`` is only visible when an
    embedding class is configured for action retrieval (= reyn.yaml
    ``action_retrieval.embedding_class`` resolves to a real entry in
    ``embedding.classes``). Callers (= router_tools.py in PR-3) pass
    the resolved embedding class name (or None) here to decide whether
    to include SEARCH_ACTIONS in tools=.
    """
    return bool(action_retrieval_embedding_class)


def is_exec_available(*, sandbox_backend: str | None) -> bool:
    """Return True iff the ``exec`` category should be exposed.

    Per FP-0034 §D14-ext, the ``exec`` category (and the ``exec__*``
    qualified names it contains) is only visible when a real sandbox
    backend is configured. ``sandbox_backend`` of ``"noop"`` or None
    keeps the category hidden so list_actions(category=["exec"])
    returns empty and the schema enum can also drop ``"exec"``.
    """
    if not sandbox_backend:
        return False
    return sandbox_backend != "noop"


def visible_categories(
    *,
    action_retrieval_embedding_class: str | None = None,
    sandbox_backend: str | None = None,
) -> tuple[str, ...]:
    """Return the categories that should be visible given the current env.

    Drops ``exec`` when ``is_exec_available`` is False. Other categories
    are always visible (search_actions visibility is a tool-level
    decision, not a category-level one).
    """
    visible: list[str] = []
    for cat in CATEGORIES:
        if cat == "exec" and not is_exec_available(sandbox_backend=sandbox_backend):
            continue
        visible.append(cat)
    return tuple(visible)


# ── 4 Universal wrapper ToolDefinitions ────────────────────────────────────
#
# Schemas follow the FP-0034 §"Universal Catalog Wrappers" section
# verbatim. Descriptions are tuned for LLM consumption (= short,
# concrete, with a usage hint pointing at the companion wrappers).


_LIST_ACTIONS_DESCRIPTION = (
    "Browse available actions in alphabetical order with optional "
    "category and text filter, paginated. Returns "
    "{items: [{qualified_name, short_description}, ...], total: int}. "
    "Use this to enumerate what is available; for semantic relevance "
    "search use search_actions (when available)."
)


_LIST_ACTIONS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "array",
            "items": {"type": "string", "enum": list(CATEGORIES)},
            "description": (
                "One or more categories to enumerate. Omit or pass an "
                "empty list to include all categories. Categories: "
                + ", ".join(CATEGORIES)
                + "."
            ),
        },
        "filter": {
            "type": "string",
            "description": (
                "Optional substring match (case-insensitive) against "
                "qualified_name and short_description."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "Pagination offset (default 0).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 20,
            "description": "Page size (default 20).",
        },
    },
}


_SEARCH_ACTIONS_DESCRIPTION = (
    "Semantic search across available actions (multilingual, "
    "embedding-based). Returns relevance-ranked top results "
    "{items: [{qualified_name, short_description}, ...]}. Available "
    "only when an embedding class is configured for action retrieval "
    "(reyn.yaml action_retrieval.embedding_class). For alphabetical "
    "browse or text-substring filter, use list_actions."
)


_SEARCH_ACTIONS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural-language query in any language.",
        },
        "category": {
            "type": "array",
            "items": {"type": "string", "enum": list(CATEGORIES)},
            "description": "Optional category restriction.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 10,
            "description": "Top-K results to return (default 10).",
        },
    },
    "required": ["query"],
}


_DESCRIBE_ACTION_DESCRIPTION = (
    "Get the long description, input schema, and metadata for one "
    "action or resource. Use the qualified_name returned by "
    "list_actions or search_actions. On unknown action_name, returns "
    "an error response with similar-name suggestions."
)


_DESCRIBE_ACTION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_name": {
            "type": "string",
            "description": (
                "Qualified name of the action/resource to describe "
                "(e.g. 'skill__code_review', 'mcp.tool__brave.search', "
                "'rag.corpus__meetings')."
            ),
        },
    },
    "required": ["action_name"],
}


_INVOKE_ACTION_DESCRIPTION = (
    "Invoke an action or resource using its canonical default semantic. "
    "Resources (mcp.server, rag.corpus, memory.entry, mcp.tool, skill, "
    "agent.peer) support invocation with their canonical operation "
    "(e.g. rag.corpus__meetings with `query` runs recall against that "
    "single source). Use describe_action(action_name) first to discover "
    "the expected input_schema. On unknown action_name, returns an error "
    "response with similar-name suggestions."
)


_INVOKE_ACTION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_name": {
            "type": "string",
            "description": (
                "Qualified name of the action/resource to invoke "
                "(e.g. 'skill__code_review', 'mcp.tool__brave.search')."
            ),
        },
        "args": {
            "type": "object",
            "description": (
                "Arguments for the action; shape comes from "
                "describe_action.input_schema. May be omitted for "
                "resources whose canonical invoke takes no args "
                "(e.g. memory.entry__foo)."
            ),
        },
    },
    "required": ["action_name"],
}


# ── Stub handlers (PR-2 will land the real dispatcher) ─────────────────────
#
# Each stub raises NotImplementedError with a clear pointer at the PR
# that will land the implementation. This lets PR-1 land the schema
# surface (= type-checkable, registry-shaped, ready for downstream
# consumers) without changing any runtime behavior. The 4 ToolDefinitions
# are NOT registered in get_default_registry() until PR-3.


async def _handle_list_actions(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Stub for list_actions; PR-2 lands the real dispatcher."""
    raise NotImplementedError(
        "list_actions dispatcher is wired in FP-0034 PR-2 "
        "(universal_dispatch.py). PR-1 establishes the ToolDefinition "
        "surface only."
    )


async def _handle_search_actions(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Stub for search_actions; PR-2 lands the real dispatcher."""
    raise NotImplementedError(
        "search_actions dispatcher is wired in FP-0034 PR-2 "
        "(ActionEmbeddingIndex landing is FP-0034 Phase 2)."
    )


async def _handle_describe_action(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Stub for describe_action; PR-2 lands the real dispatcher."""
    raise NotImplementedError(
        "describe_action dispatcher is wired in FP-0034 PR-2 "
        "(universal_dispatch.py qualified-name routing)."
    )


async def _handle_invoke_action(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Stub for invoke_action; PR-2 lands the real dispatcher."""
    raise NotImplementedError(
        "invoke_action dispatcher is wired in FP-0034 PR-2 "
        "(universal_dispatch.py qualified-name routing + D19 resource "
        "invoke canonical semantic)."
    )


# ── 4 ToolDefinitions exported ─────────────────────────────────────────────


LIST_ACTIONS = ToolDefinition(
    name="list_actions",
    description=_LIST_ACTIONS_DESCRIPTION,
    parameters=_LIST_ACTIONS_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_list_actions,
    category="discovery",
    purity="read_only",
)


SEARCH_ACTIONS = ToolDefinition(
    name="search_actions",
    description=_SEARCH_ACTIONS_DESCRIPTION,
    parameters=_SEARCH_ACTIONS_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_search_actions,
    category="discovery",
    purity="read_only",
)


DESCRIBE_ACTION = ToolDefinition(
    name="describe_action",
    description=_DESCRIBE_ACTION_DESCRIPTION,
    parameters=_DESCRIBE_ACTION_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_describe_action,
    category="discovery",
    purity="read_only",
)


INVOKE_ACTION = ToolDefinition(
    name="invoke_action",
    description=_INVOKE_ACTION_DESCRIPTION,
    parameters=_INVOKE_ACTION_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_invoke_action,
    category="invocation",
    purity="side_effect",
)


__all__ = [
    "CATEGORIES",
    "LIST_ACTIONS",
    "SEARCH_ACTIONS",
    "DESCRIBE_ACTION",
    "INVOKE_ACTION",
    "split_qualified_name",
    "build_qualified_name",
    "is_valid_qualified_name",
    "is_search_available",
    "is_exec_available",
    "visible_categories",
]
