"""Universal catalog wrappers — FP-0034 Phase 1 foundation + PR-3a wiring.

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

PR-1 (landed): type surface only — 4 ToolDefinitions with stub
handlers, qualified-name parse / build / validate, 13-category enum,
D14 visibility-gating helpers.

PR-2 (landed): pure routing layer — ``universal_dispatch.py`` with
resolve_invoke_action / resolve_describe_action / suggest_similar_names.

PR-3a (this commit): wire real handlers — list_actions /
describe_action / invoke_action handlers delegate via the PR-2 routing
+ the unified ToolRegistry. ``search_actions`` remains a stub (= depends
on Phase 2 embedding index). The 4 wrappers are NOT yet added to the
router's tools= (= that lands in PR-3b). Registry registration is
landed so any caller iterating the registry sees the wrappers.

PR-3b (later): router tools= placement + SP refactor (D9
category-only description); build_tools() shape change.

PR-4 (later): new op ``mcp.operation__drop_server`` for the destructor
side of MCP server CRUD (D23).

PR-5 (later): Tier 3 LLMReplay fixtures + e2e verification of §Phase 1
verification 1-9.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Lazy-imported at function-body level to break the circular dependency
# with universal_dispatch.py (which imports CATEGORIES + split_qualified_name
# from this module). The handlers below import the dispatch symbols inside
# their function bodies; this typing-time alias is for type checkers only.
if TYPE_CHECKING:
    from reyn.tools.universal_dispatch import UnknownActionError


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


# ── Handler implementation helpers ────────────────────────────────────────


_MAX_SHORT_DESC: Final[int] = 200


def _truncate_short_description(desc: str | None) -> str:
    """Trim long descriptions for the list_actions / search_actions output.

    list_actions returns ``short_description``, distinct from
    describe_action's full description. The cap keeps the LLM-visible
    payload small even when target ToolDefinitions ship verbose docs.
    """
    if not desc:
        return ""
    if len(desc) <= _MAX_SHORT_DESC:
        return desc
    return desc[: _MAX_SHORT_DESC - 1].rstrip() + "…"


def _build_error_response(exc: "UnknownActionError") -> dict[str, Any]:
    """Format an UnknownActionError into the §D12 LLM-facing response shape.

    FP-0034 §D12 specifies the LLM sees an ``error`` message, the
    offending ``action_name``, a list of ``suggestions``, and a ``hint``
    pointing at the recovery path (= list_actions / describe_action).
    PR-3a returns this verbatim so the LLM can recover in 1 turn.
    """
    return {
        "error": str(exc),
        "action_name": exc.action_name,
        "reason": exc.reason,
        "suggestions": list(exc.suggestions),
        "hint": (
            "Use list_actions(category=...) to discover available "
            "actions, then describe_action(action_name) to fetch the "
            "input schema."
        ),
    }


def _missing_action_name_error() -> dict[str, Any]:
    """Error response when caller omits action_name (= required field)."""
    return {
        "error": "action_name is required",
        "action_name": None,
        "reason": "action_name parameter was not provided",
        "suggestions": [],
        "hint": (
            "Provide action_name (qualified, e.g. 'skill__code_review') "
            "from list_actions or search_actions output."
        ),
    }


def _enumerate_static_category(category: str) -> list[dict[str, str]]:
    """Enumerate qualified names for a STATIC operation category.

    Static categories (file / web / memory.operation / reyn.source /
    rag.operation) have known qualified names declared in
    universal_dispatch._OPERATION_RULES. Their short_description comes
    from the target ToolDefinition in the registry.

    Resource categories (skill / agent.peer / mcp.{server,tool} /
    memory.entry / rag.corpus) are NOT handled here — they need caller
    state (= ctx.router_state.available_*). See _enumerate_category.
    """
    # Lazy imports to avoid circular dependency (universal_dispatch imports
    # CATEGORIES + split_qualified_name from THIS module).
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import (
        UnknownActionError,
        known_qualified_name_for_category,
        resolve_describe_action,
    )

    registry = get_default_registry()
    out: list[dict[str, str]] = []
    for qualified_name in known_qualified_name_for_category(category):
        try:
            resolved = resolve_describe_action(qualified_name)
        except UnknownActionError:
            continue
        target = registry.lookup(resolved.target_tool_name)
        short = _truncate_short_description(
            target.description if target is not None else "",
        )
        out.append({
            "qualified_name": qualified_name,
            "short_description": short,
        })
    return out


def _enumerate_category(category: str, ctx: ToolContext) -> list[dict[str, str]]:
    """Enumerate qualified names for ``category`` consulting caller state.

    Dispatch by category kind:
      - Static operation categories (file / web / memory.operation /
        reyn.source / rag.operation / mcp.operation) →
        _enumerate_static_category (= populated via universal_dispatch's
        ``_OPERATION_RULES`` table)
      - Resource categories → consult ctx.router_state (skills /
        agents / mcp_servers / mcp_servers[*].tools / list_memory_fn)
      - Categories without state-binding yet (rag.corpus / exec) →
        empty list (Phase 2 will populate via embedding-backed index
        and sandbox-backed exec enumeration)

    The output items each carry ``qualified_name`` (= what
    invoke_action / describe_action expects) and ``short_description``
    (= LLM-facing summary, truncated per _MAX_SHORT_DESC).
    """
    rs = ctx.router_state

    if category in (
        "file", "web", "memory.operation", "reyn.source", "rag.operation",
        "mcp.operation",
    ):
        return _enumerate_static_category(category)

    if category == "skill":
        if rs is None or not rs.available_skills:
            return []
        return [
            {
                "qualified_name": build_qualified_name("skill", s["name"]),
                "short_description": _truncate_short_description(
                    s.get("description", ""),
                ),
            }
            for s in rs.available_skills
            if isinstance(s, Mapping) and "name" in s
        ]

    if category == "agent.peer":
        if rs is None or not rs.available_agents:
            return []
        return [
            {
                "qualified_name": build_qualified_name("agent.peer", a["name"]),
                "short_description": _truncate_short_description(
                    a.get("role") or a.get("description", ""),
                ),
            }
            for a in rs.available_agents
            if isinstance(a, Mapping) and "name" in a
        ]

    if category == "mcp.server":
        if rs is None or not rs.mcp_servers:
            return []
        return [
            {
                "qualified_name": build_qualified_name("mcp.server", s["name"]),
                "short_description": _truncate_short_description(
                    s.get("description", ""),
                ),
            }
            for s in rs.mcp_servers
            if isinstance(s, Mapping) and "name" in s
        ]

    if category == "mcp.tool":
        if rs is None or not rs.mcp_servers:
            return []
        out: list[dict[str, str]] = []
        for srv in rs.mcp_servers:
            if not isinstance(srv, Mapping):
                continue
            srv_name = srv.get("name")
            if not srv_name:
                continue
            tools = srv.get("tools") or []
            for tool in tools:
                if not isinstance(tool, Mapping):
                    continue
                tool_name = tool.get("name")
                if not tool_name:
                    continue
                qn = build_qualified_name(
                    "mcp.tool", f"{srv_name}.{tool_name}",
                )
                out.append({
                    "qualified_name": qn,
                    "short_description": _truncate_short_description(
                        tool.get("description", ""),
                    ),
                })
        return out

    if category == "memory.entry":
        if rs is None or rs.list_memory_fn is None:
            return []
        try:
            entries = rs.list_memory_fn("") or []
        except Exception:
            return []
        out2: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            if not name:
                continue
            out2.append({
                "qualified_name": build_qualified_name("memory.entry", name),
                "short_description": _truncate_short_description(
                    entry.get("description", ""),
                ),
            })
        return out2

    # rag.corpus / exec — Phase 2 will populate (embedding-backed
    # corpus list + sandbox-backed exec enumeration).
    return []


# ── Real handlers (PR-3a) ─────────────────────────────────────────────────


async def _handle_list_actions(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """list_actions handler — alphabetical browse with filter + pagination.

    Per FP-0034 §D11, returns:
      ``{items: [{qualified_name, short_description}, ...], total: int}``

    Sort is alphabetical by qualified_name (= pagination stability).
    The ``filter`` substring matches case-insensitively against
    qualified_name AND short_description. Pagination uses offset+limit
    REST conventions.
    """
    # Resolve category filter — empty / unset = all visible categories
    category_filter = args.get("category") or []
    if isinstance(category_filter, str):
        category_filter = [category_filter]
    if category_filter:
        categories = [c for c in category_filter if c in CATEGORIES]
    else:
        categories = list(CATEGORIES)

    text_filter = (args.get("filter") or "").lower()
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = max(1, int(args.get("limit", 20) or 20))

    items: list[dict[str, str]] = []
    for cat in categories:
        items.extend(_enumerate_category(cat, ctx))

    if text_filter:
        items = [
            it for it in items
            if text_filter in it["qualified_name"].lower()
            or text_filter in it["short_description"].lower()
        ]

    # Alphabetical sort for pagination stability (§D11)
    items.sort(key=lambda it: it["qualified_name"])
    total = len(items)
    page = items[offset:offset + limit]

    return {"items": page, "total": total}


async def _handle_search_actions(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """search_actions handler — STUB until FP-0034 Phase 2 lands embedding.

    Per §D14, search_actions is visible only when embedding is
    configured. The visibility gate lives at the integration layer
    (= router_tools.py in PR-3b). When the handler is reached without
    embedding wired, it raises NotImplementedError pointing at Phase 2.
    """
    raise NotImplementedError(
        "search_actions semantic search lands in FP-0034 Phase 2 "
        "(ActionEmbeddingIndex + embedding-based ranking). PR-3a wires "
        "list_actions / describe_action / invoke_action only."
    )


async def _handle_describe_action(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """describe_action handler — return target's description + input_schema.

    Per FP-0034 §D11, returns ``{long_description?, input_schema,
    metadata?}``. PR-3a maps to the target ToolDefinition's fields:
      - description (= the target's LLM-facing description)
      - input_schema (= the target's parameters dict)
      - metadata (= qualified_name + target_tool_name + category + purity)

    For unknown qualified_name, returns the §D12 error-with-suggestions
    response.
    """
    qualified_name = args.get("action_name")
    if not qualified_name:
        return _missing_action_name_error()

    # Lazy imports for circular-dep safety
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import (
        UnknownActionError,
        resolve_describe_action,
    )

    try:
        resolved = resolve_describe_action(qualified_name)
    except UnknownActionError as exc:
        # Augment suggestions with router_state-aware candidates
        return _build_error_response(_augment_suggestions(exc, ctx))

    registry = get_default_registry()
    target = registry.lookup(resolved.target_tool_name)
    if target is None:
        return _build_error_response(UnknownActionError(
            qualified_name,
            f"target tool {resolved.target_tool_name!r} is not in the "
            f"registry (PR-3a wires the canonical surface; if you see "
            f"this in production, the target may be a future-PR op)",
        ))

    return {
        "qualified_name": qualified_name,
        "description": target.description,
        "input_schema": dict(target.parameters),
        "metadata": {
            "target_tool_name": resolved.target_tool_name,
            "category": target.category,
            "purity": target.purity,
        },
    }


async def _handle_invoke_action(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """invoke_action handler — delegate to target via PR-2 routing.

    PR-3a wiring:
      1. Resolve qualified_name → target_tool_name + transformed_args
         via universal_dispatch.resolve_invoke_action.
      2. Look up target ToolDefinition in the unified registry.
      3. Invoke target.handler(transformed_args, ctx).

    The ToolContext is forwarded verbatim so router_state callbacks
    (= run_skill_fn / spawn_skill_fn / send_to_agent / op_context_factory
    / list_memory_fn / etc.) reach the target handler as if the caller
    had invoked it directly. This is what makes invoke_action a
    transparent wrapper rather than a separate execution path.

    Unknown qualified_name → §D12 error-with-suggestions response.
    """
    qualified_name = args.get("action_name")
    if not qualified_name:
        return _missing_action_name_error()

    inner_args = args.get("args") or {}

    # Lazy imports for circular-dep safety
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import (
        UnknownActionError,
        resolve_invoke_action,
    )

    try:
        resolved = resolve_invoke_action(qualified_name, inner_args)
    except UnknownActionError as exc:
        return _build_error_response(_augment_suggestions(exc, ctx))

    registry = get_default_registry()
    target = registry.lookup(resolved.target_tool_name)
    if target is None:
        return _build_error_response(UnknownActionError(
            qualified_name,
            f"target tool {resolved.target_tool_name!r} is not in the "
            f"registry (PR-3a wires the canonical surface; if you see "
            f"this in production, the target may be a future-PR op)",
        ))

    # Forward ctx verbatim — target handlers consume their slice of
    # router_state / phase_state via the typed sub-objects.
    return await target.handler(resolved.target_args, ctx)


def _augment_suggestions(
    exc: "UnknownActionError", ctx: ToolContext,
) -> "UnknownActionError":
    """Re-suggest using router_state-aware candidates when available.

    The PR-2 default suggestion pool is the static catalogue
    (= KNOWN_STATIC_QUALIFIED_NAMES, 13 names). When ``ctx.router_state``
    is populated, we widen the pool with dynamic items (= skills /
    agents / mcp.tool / mcp.server / memory.entry) so the suggestion
    surfaces names the LLM can actually invoke. Falls back to the
    original exception unchanged when no dynamic items exist.
    """
    # Lazy import for circular-dep safety
    from reyn.tools.universal_dispatch import (
        UnknownActionError as _UnknownActionError,
    )
    from reyn.tools.universal_dispatch import (
        suggest_similar_names,
    )

    candidates: list[str] = []
    for cat in CATEGORIES:
        for item in _enumerate_category(cat, ctx):
            candidates.append(item["qualified_name"])

    if not candidates:
        return exc

    new_suggestions = suggest_similar_names(
        exc.action_name, candidates=candidates,
    )
    return _UnknownActionError(
        exc.action_name, exc.reason, suggestions=new_suggestions,
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
