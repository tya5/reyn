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

from typing import TYPE_CHECKING, Any, Collection, Final, Mapping

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
    # Phase 1 follow-up (2026-05-25): collapsed ``agent.peer`` resource
    # category into ``multi_agent`` verb category (= list_peers /
    # describe_peer / delegate). Same shape rationale as #879 mcp
    # collapse — resource entries (agent names) → verb actions whose
    # args carry the agent name explicitly.
    "multi_agent",
    # Issue #879: collapsed the previous mcp.server / mcp.tool /
    # mcp.operation sub-categories + skill__mcp_search / skill__mcp_install
    # into a single ``mcp`` category. 2026-05-25: install surface
    # further split along the source axis into 3 verbs (registry /
    # package / local). Full verb set: mcp__search_registry,
    # mcp__install_registry, mcp__install_package, mcp__install_local,
    # mcp__list_servers, mcp__list_tools, mcp__call_tool,
    # mcp__drop_server. See universal_dispatch._OPERATION_RULES.
    "mcp",
    "file",
    "web",
    "memory_entry",
    "memory_operation",
    "reyn_source",
    "rag_corpus",
    "rag_operation",
    "validation",
    "exec",
)


# The qualified-name separator. Double-underscore is chosen so a dotted
# entry name (``brave.search``) never collides with the boundary; see
# FP-0034 §D18. (#1456: category names are now dot-free — alnum/_/- only,
# per the provider function-name grammar; entry names may still carry dots.)
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
        ``rag_corpus__meetings``     → ("rag_corpus", "meetings")

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


def is_search_available(
    *,
    action_retrieval_embedding_class: str | None,
    embedding_class_names: "Collection[str] | None" = None,
) -> bool:
    """Return True iff ``search_actions`` should be exposed to the LLM.

    Per FP-0034 §D14, ``search_actions`` is only visible when an embedding
    class is configured for action retrieval AND that class is a real entry
    in ``embedding.classes`` (a class-typed field is closed-world).

    #1454: the primary membership reconciliation happens upstream at config
    load (``_reconcile_embedding_class`` degrades a dangling class to None +
    logs once). By the time this is called the value is normally already
    clean, so the ``bool()`` check suffices. ``embedding_class_names`` is the
    belt-and-suspenders leg: when a caller passes the known class names, a
    non-member class returns False here too (closed-world enforced at the
    visibility boundary, not just at config load). No logging here — the single
    actionable log lives in the config-load reconciliation to avoid double
    surfacing.
    """
    if not action_retrieval_embedding_class:
        return False
    if (
        embedding_class_names is not None
        and action_retrieval_embedding_class not in embedding_class_names
    ):
        return False
    return True


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
    "WHAT: Discover actions in the FULL catalog (= a superset of the hot-list "
    "function entries you can see directly). The hot-list shown in your "
    "function list is a curated subset; this tool reveals the rest. "
    "Filter by category: `category=[...]` array (enum-restricted, exact "
    "category match). Omit or pass [] to enumerate everything visible. "
    "Returns {items: [{qualified_name, short_description}, ...], total: int}. "
    "An empty items array means no actions match — report this honestly. "
    "WHEN: PREFERRED FIRST for known-category enumeration (e.g. 'show me all "
    "memory_entry actions', 'what skills are available?') or exact-name "
    "lookup when you already know the category but not the exact entry. "
    "ALWAYS call list_actions BEFORE refusing a category-listable capability "
    "request. Refusing without a list_actions check is a FAILURE MODE "
    "(= the action you assumed missing may exist behind the hot-list). "
    "For known-category enumeration pass `category=['exec']` to narrow. "
    "WHEN NOT: For semantic / natural-language / free-text discovery (e.g. "
    "'find an action that can ...', '探したい', '関連する', 'something for X') "
    "use search_actions instead — it returns relevance-ranked matches across "
    "categories rather than a flat enumeration. If you already know the "
    "exact action name, skip both and call invoke_action directly. "
    "PREFERRED OVER: Guessing action names + refusing capability requests — "
    "list_actions returns the canonical qualified names (e.g. "
    "skill__code_review, mcp__call_tool) that invoke_action and "
    "describe_action expect. "
    "POST_CALL: After list_actions reveals at least one matching action, you "
    "MUST follow with describe_action or invoke_action. Do NOT reply directly "
    "— silent stop after enumeration is a failure mode. When items is empty, "
    "honestly tell the user no matching actions are available."
)


_LIST_ACTIONS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "array",
            "items": {"type": "string", "enum": list(CATEGORIES)},
            "description": (
                "Filter by category. Pass an array of category names "
                "(e.g. category=['exec'], category=['skill', 'file']). "
                "Omit or pass [] to include all categories. "
                "Categories: " + ", ".join(CATEGORIES) + "."
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
            "default": 10,
            "description": "Page size (default 10).",
        },
    },
}


_SEARCH_ACTIONS_DESCRIPTION = (
    "WHAT: Semantic search across available actions — multilingual, "
    "embedding-based, relevance-ranked. "
    "Returns {items: [{qualified_name, short_description, score}, ...]}. "
    "WHEN: PREFERRED FIRST for semantic / natural-language / free-text "
    "queries — when the user asks to 探す / 探したい / 関連 / similar to / "
    "something for X / actions about Y / find ... related to Z, or describes "
    "an intent without naming a specific category. ALWAYS call search_actions "
    "BEFORE refusing a semantic-intent capability request. Refusing without "
    "a search_actions check is a FAILURE MODE (= relevance ranking may surface "
    "the action across categories that a flat enumeration would miss). "
    "Multilingual — works in any language (Japanese, English, etc.). "
    "Handles both semantic descriptions AND free-text keyword lookup "
    "(e.g. 'http' を含む action). "
    "WHEN NOT: For known-category enumeration (e.g. 'show me all skills', "
    "「memory_entry の一覧」) use list_actions(category=[...]) instead — "
    "it returns the flat catalogue slice rather than relevance-ranked hits. "
    "If you already know the exact action name, skip both and call "
    "invoke_action directly. "
    "Available only when an embedding class is configured (reyn.yaml "
    "action_retrieval.embedding_class). "
    "POST_CALL: After search_actions reveals at least one matching action, "
    "you MUST follow with describe_action or invoke_action. Do NOT reply "
    "directly — silent stop after semantic search is a failure mode."
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
    "WHAT: Get the full description, input schema, and metadata for one action "
    "or resource. Returns {description, input_schema, metadata}. "
    "WHEN: Use this before invoke_action when you need to know the exact "
    "argument shape of an action. Should be called whenever you have the "
    "qualified_name but are unsure of the required args. "
    "WHEN NOT: If you already know the input schema (from a previous call or "
    "the action takes no args), skip this and call invoke_action directly. "
    "PREFERRED OVER: Guessing argument names — describe_action returns the "
    "authoritative input_schema. On unknown action_name, returns an error "
    "with similar-name suggestions. "
    "POST_CALL: After describe_action, you MUST follow with invoke_action or "
    "explain in text why not. Never stop silently after investigation."
)


_DESCRIBE_ACTION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_name": {
            "type": "string",
            "description": (
                "Qualified name of the action/resource to describe "
                "(e.g. 'skill__code_review', 'mcp.tool__brave.search', "
                "'rag_corpus__meetings')."
            ),
        },
    },
    "required": ["action_name"],
}


_INVOKE_ACTION_DESCRIPTION = (
    "WHAT: Execute an action by qualified name (<category>__<entry>). "
    "Executes the action's default semantic operation. "
    "WHEN: Call this whenever you intend to run any action — skill, MCP tool, "
    "file operation, web search, memory write, recall, etc. All catalog actions "
    "are invoked through this single entry point. "
    "WHEN NOT: For chitchat or self-questions, reply without tools. "
    "PREFERRED OVER: Legacy per-kind tools (invoke_skill, call_mcp_tool, etc.) — "
    "invoke_action covers all 13 action categories uniformly. "
    "On unknown action_name, returns an error with similar-name suggestions. "
    ""
    "SPAWN-ACK HANDLING: when an action result is {status:'spawned', ...}, the "
    "router exits the current turn before this tool description applies; the OS "
    "emits the user-visible acknowledgment directly. You will not be asked to "
    "compose a reply for the spawn-ack turn. "
    ""
    "TASK_SPAWNED: an agent-role message starting with [task_spawned] is "
    "OS-emitted when an async task is launched (kind=skill | plan, paired "
    "with run_id for skill or plan_id for plan). The structured header "
    "lets you correlate the spawn with the later [task_completed] message "
    "carrying the same identifier. The trailing human-readable line is "
    "what the user sees; the header is your correlation record. "
    ""
    "TASK_COMPLETED: a user-role message starting with [task_completed] is "
    "OS-injected when a previously-spawned async task finishes "
    "(kind=skill | plan). The message carries the task's status + result "
    "fields. status='finished' means normal completion; other values "
    "('loop_limit_exceeded', 'phase_budget_exceeded', 'budget_exceeded', "
    "'error', or any non-'finished' value with result.error present) "
    "indicate the task did not complete normally. "
    ""
    "AGENT DELEGATION: For peer agent delegation, use "
    "action_name='multi_agent__delegate' with args {to: '<agent_name>', "
    "request: ...}; get its canonical args via "
    "describe_action(action_name='multi_agent__delegate'). "
    "Use when task is outside available actions but matches a peer agent's role, "
    "or when user explicitly addresses a named agent. "
    "Acknowledge delegation in 1 sentence."
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
                "(e.g. memory_entry__foo)."
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

    Static categories (file / web / memory_operation / reyn_source /
    rag_operation) have known qualified names declared in
    universal_dispatch._OPERATION_RULES. Their short_description comes
    from the target ToolDefinition in the registry.

    Resource categories (skill / agent.peer / mcp.{server,tool} /
    memory_entry / rag_corpus) are NOT handled here — they need caller
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


def _mcp_tool_qualified_name(server: str, tool: str) -> str:
    """The first-class per-tool action name (#1647): ``mcp__<server>__<tool>``.

    Single-sourced so enumeration + describe + the dispatch split all agree on
    the ``<server>__<tool>`` identifier form (double-underscore boundary, the
    same form ``mcp_call_tool`` splits on)."""
    return build_qualified_name("mcp", f"{server}__{tool}")


def _enumerate_mcp_tools(rs: Any) -> list[dict[str, str]]:
    """Per-tool MCP actions ``mcp__<server>__<tool>`` from the cached snapshot.

    #1647: reads ``rs.mcp_servers`` — shape ``[{name, description,
    tools?: [{name, description, inputSchema}]}]`` — which RouterLoop fills from
    the FP-0037 per-session ``_mcp_tools_cache`` (probed once on the first turn,
    disk-warm-started). No live probe here: enumeration is a pure read of the
    cached snapshot, so list_actions / hot-list / retrieval do not re-fetch
    (FP-0034 caching req). ``tools`` is absent until the cache is warm → graceful
    empty. Each cached tool ``name`` is the BARE server-side tool name; the
    qualified action is ``mcp__<server>__<tool>``."""
    if rs is None:
        return []
    servers = getattr(rs, "mcp_servers", None) or []
    out: list[dict[str, str]] = []
    for server in servers:
        if not isinstance(server, Mapping):
            continue
        sname = server.get("name")
        tools = server.get("tools")
        if not sname or not tools:
            continue
        for t in tools:
            if not isinstance(t, Mapping):
                continue
            tname = t.get("name")
            if not tname:
                continue
            out.append({
                "qualified_name": _mcp_tool_qualified_name(str(sname), str(tname)),
                "short_description": _truncate_short_description(
                    t.get("description", ""),
                ),
            })
    return out


def _enumerate_category(category: str, ctx: ToolContext) -> list[dict[str, str]]:
    """Enumerate qualified names for ``category`` consulting caller state.

    Dispatch by category kind:
      - Static operation categories (file / web / memory_operation /
        reyn_source / rag_operation / mcp.operation) →
        _enumerate_static_category (= populated via universal_dispatch's
        ``_OPERATION_RULES`` table)
      - Resource categories → consult ctx.router_state (skills /
        agents / mcp_servers / mcp_servers[*].tools / list_memory_fn /
        available_rag_sources)
      - Categories without state-binding yet (exec) →
        empty list (Phase 2 will populate via sandbox-backed exec
        enumeration once the introspection API lands)

    The output items each carry ``qualified_name`` (= what
    invoke_action / describe_action expects) and ``short_description``
    (= LLM-facing summary, truncated per _MAX_SHORT_DESC).
    """
    rs = ctx.router_state

    # #1667: explicit per-session category exclusion. The task-agent / external-repo
    # eval path (e.g. SWE-bench on /testbed) sets ``excluded_categories`` so a
    # category irrelevant to the task — Reyn's own ``reyn_source`` self-help surface
    # — does not compete with ``file__*`` for the weak model's selection. Applied at
    # the catalog SOURCE so the category vanishes UNIFORMLY from ``catalog_entries``
    # (every scheme's flat list: codeact code-API / enumerate-all / retrieval) +
    # ``list_actions`` + dispatch — a top-level ``exclude_tools`` name filter cannot
    # reach this. The general/interactive agent leaves it empty and keeps the
    # category (self-help preserved). P7-clean: the excluded set is caller data, no
    # hardcoded category name here.
    excluded = getattr(rs, "excluded_categories", None) or frozenset()
    if category in excluded:
        return []

    if category in (
        "file", "web", "memory_operation", "reyn_source", "rag_operation",
        "multi_agent", "validation",
    ):
        return _enumerate_static_category(category)

    # #1647: the ``mcp`` category carries BOTH the static management verbs
    # (mcp__call_tool / mcp__list_tools / mcp__install_* / …) AND first-class
    # per-tool actions ``mcp__<server>__<tool>`` for every tool on a connected
    # server (from the FP-0037 cached snapshot on router_state). The per-tool
    # actions make each MCP tool selectable by name with its real inputSchema,
    # superseding the generic call_mcp_tool double-args foot-gun (#1646).
    if category == "mcp":
        items = list(_enumerate_static_category("mcp"))
        items.extend(_enumerate_mcp_tools(rs))
        return items

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

    if category == "rag_corpus":
        # FP-0034 Phase 2 prep: enumerate indexed RAG corpora from the
        # router caller state snapshot.  RouterLoop populates this from
        # ``SourceManifest.get_all()`` once per loop iteration; the
        # handler reads it without a fresh manifest round-trip.
        if rs is None or not rs.available_rag_sources:
            return []
        return [
            {
                "qualified_name": build_qualified_name("rag_corpus", c["name"]),
                "short_description": _truncate_short_description(
                    c.get("description", ""),
                ),
            }
            for c in rs.available_rag_sources
            if isinstance(c, Mapping) and "name" in c
        ]

    if category == "memory_entry":
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
                "qualified_name": build_qualified_name("memory_entry", name),
                "short_description": _truncate_short_description(
                    entry.get("description", ""),
                ),
            })
        return out2

    # exec category — sandboxed_exec (FP-0017).
    # Visible only when a real sandbox backend is configured (D14-ext).
    # RouterCallerState.sandbox_backend carries the backend name (None = noop).
    if category == "exec":
        if rs is None:
            return []
        backend = getattr(rs, "sandbox_backend", None)
        if not is_exec_available(sandbox_backend=backend):
            return []
        return [
            {
                "qualified_name": "exec__sandboxed_exec",
                "short_description": (
                    "Execute a command in a sandboxed environment."
                ),
            }
        ]

    return []


# ── Category validation (#934 stale-enum explicit error) ────────────────────
#
# LLM providers vary in how strictly they enforce a JSON-Schema ``enum`` on a
# tool argument. In practice an LLM whose training-data catalog snapshot
# pre-dates one of Reyn's category collapses (#882 mcp / #909 multi_agent /
# etc.) passes a stale name like ``"mcp.server"`` through to the handler.
# Pre-#934 the handlers silently dropped unknown entries from ``category=[…]``
# and returned an empty result; the LLM had no recovery cue.
#
# Post-#934 the handlers surface an explicit error envelope that lists the
# current valid categories AND maps the legacy names to their replacement,
# so the LLM can self-correct in a single retry without further inference.

_LEGACY_CATEGORY_REDIRECTS: Final[dict[str, str]] = {
    # PR #882 — mcp.server / mcp.tool / mcp.operation collapsed into a single
    # ``mcp`` verb category.
    "mcp.server": "mcp",
    "mcp.tool": "mcp",
    "mcp.operation": "mcp",
    # PR #909 — agent.peer resource category collapsed into ``multi_agent``
    # operation category (= multi_agent__list_peers / __describe_peer /
    # __delegate).
    "agent.peer": "multi_agent",
}


def _unknown_categories_error(unknowns: list[str]) -> dict[str, Any]:
    """Build the error envelope returned when ``category=[…]`` carries an
    unknown name.

    The message inlines (a) the full current ``CATEGORIES`` list and (b) any
    legacy→current mapping that matches an unknown entry. The mapping is the
    load-bearing part: a bare valid-list forces the LLM to do a "which is
    the new name" inference round-trip; the inline mapping enables
    single-turn self-correction. See #934 design rationale (= sandbox_2
    B57 W6-S3-style observation).
    """
    valid_list = ", ".join(repr(c) for c in CATEGORIES)
    redirects = [
        f"{legacy!r} → {current!r}"
        for legacy in unknowns
        if (current := _LEGACY_CATEGORY_REDIRECTS.get(legacy)) is not None
    ]
    redirect_block = ""
    if redirects:
        redirect_block = (
            "\n\nLegacy categories from prior collapse refactors:\n  "
            + "\n  ".join(redirects)
        )
    return {
        "error": (
            f"unknown category {unknowns[0]!r}"
            if len(unknowns) == 1
            else f"unknown categories {unknowns!r}"
        ),
        "reason": (
            f"category names must be one of: {valid_list}.{redirect_block}"
        ),
        "hint": (
            "Re-call with `category=[<valid name>]`. Use list_actions() with "
            "no category argument to enumerate everything visible."
        ),
        "unknown": list(unknowns),
        "valid": list(CATEGORIES),
    }


def _validate_category_filter(
    raw: "list[str] | str | None",
) -> "tuple[list[str], dict[str, Any] | None]":
    """Normalise + validate the ``category=[…]`` argument.

    Returns ``(normalised_list, error_envelope_or_None)``. When the
    returned envelope is non-None, the handler must surface it verbatim
    instead of proceeding with enumeration / search — every entry the
    LLM supplied must be a current category for the call to succeed.
    """
    if not raw:
        return [], None
    if isinstance(raw, str):
        raw = [raw]
    unknowns = [c for c in raw if c not in CATEGORIES]
    if unknowns:
        return [], _unknown_categories_error(unknowns)
    return list(raw), None


# ── Hidden-state hint (FP-0043 Component C.1) ──────────────────────────────
#
# When ``search_actions`` is gated out of ``tools=`` (= operator hasn't
# configured ``action_retrieval.embedding_class``, or the embedding
# class points at a backend whose extras aren't installed), the LLM
# has no way to discover that semantic search exists. ``list_actions``
# is the discovery wrapper the LLM does see; we attach a ``hint`` field
# to its response so the LLM can surface the install / config path
# back to the user. This is the "self-service onboarding" bridge in
# FP-0043 §Component C.

_HIDDEN_STATE_HINT: Final[str] = (
    "Semantic action search (`search_actions`) is currently unavailable "
    "in this session. To enable it, run ONE of:\n"
    "  - `pip install 'reyn[local-embed]'` — local sentence-transformers "
    "model, no credentials, ~22MB one-time download (recommended for "
    "first-time users)\n"
    "  - add to reyn.yaml: `action_retrieval:\\n  embedding_class: standard`"
    " — uses OpenAI embeddings, requires `OPENAI_API_KEY`\n"
    "Until enabled, use `list_actions(category=[...])` to browse the "
    "catalog by category and `describe_action(action_name=...)` to inspect "
    "a specific action."
)


def _search_actions_ready(rs: Any) -> bool:
    """Return True iff ``search_actions`` would currently serve queries.

    The check mirrors the router-side §D14 visibility gate (= idx
    configured + provider + model class + index is_ready) but stays
    local to the catalog module so the hint logic doesn't need to
    re-import router internals.

    A None ``rs`` means we're outside a real session (= unit test /
    standalone caller); the caller decides whether to suppress the
    hint in that case via the production-context check below.
    """
    if rs is None:
        return False
    idx = getattr(rs, "action_embedding_index", None)
    provider = getattr(rs, "embedding_provider", None)
    model_class = getattr(rs, "embedding_model_class", None)
    if idx is None or provider is None or not model_class:
        return False
    is_ready = getattr(idx, "is_ready", None)
    if not callable(is_ready):
        return False
    try:
        return bool(is_ready())
    except Exception:
        return False


def _should_inject_hidden_state_hint(rs: Any) -> bool:
    """Return True iff the hint should be added to a list_actions response.

    Fires when (a) a production-context router_state is present (=
    Session-mediated; rules out pure unit-test contexts that
    don't construct an rs at all) AND (b) search_actions is not
    currently usable. Pure-test contexts (``rs is None``) are
    explicitly excluded so test fixtures + LLMReplay don't drift.

    Brief false-positives during the background index build (= rs is
    present but idx.is_ready() returns False yet) are acceptable —
    the hint is informational, not blocking; the LLM may surface
    "install local-embed" once during boot, then stop on subsequent
    turns once the index becomes ready.
    """
    if rs is None:
        return False
    return not _search_actions_ready(rs)


# ── Real handlers (PR-3a) ─────────────────────────────────────────────────


async def _handle_list_actions(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """list_actions handler — alphabetical browse with category +
    pagination.

    Per FP-0034 §D11 + #1455 uniform enrich, returns:
      ``{items: [{qualified_name, short_description, description, input_schema},
      ...], total: int}`` — EVERY page item is enriched via ``_describe_one``
      (no longer gated to category-narrowed browses), so an unfiltered browse is
      as actionable as a narrowed one. Token-bounded by the page limit (default
      10).

    Sort is alphabetical by qualified_name (= pagination stability).
    Pagination uses offset+limit REST conventions (default limit 10).

    #934: when ``category=[…]`` carries a name not in the current
    ``CATEGORIES`` tuple (= LLM-training-time stale enum), the handler
    returns an explicit error envelope instead of silently filtering.

    FP-0043 Component C.1: when ``search_actions`` is gated out of
    ``tools=`` in the current session, a ``hint`` field is added to
    the response so the LLM can surface install / config instructions
    to the user. Pure-test contexts (``router_state=None``) don't
    receive the hint so fixture replay stays byte-stable.
    """
    # Validate category filter — surface stale-enum errors explicitly.
    raw_filter = args.get("category") or []
    valid_filter, err = _validate_category_filter(raw_filter)
    if err is not None:
        return err
    categories = valid_filter if valid_filter else list(CATEGORIES)

    offset = max(0, int(args.get("offset", 0) or 0))
    limit = max(1, int(args.get("limit", 10) or 10))

    items: list[dict[str, str]] = []
    for cat in categories:
        items.extend(_enumerate_category(cat, ctx))

    # Alphabetical sort for pagination stability (§D11)
    items.sort(key=lambda it: it["qualified_name"])
    total = len(items)
    page = items[offset:offset + limit]

    # Stage B (#187) + #1455 uniform enrich: enrich EVERY page item with the
    # SAME full description + input_schema describe_action returns — via the
    # shared _describe_one, so list ≡ describe BY CONSTRUCTION — giving the LLM
    # selection-grade detail (name + description + schema) without a separate
    # describe_action round-trip (which weak models rarely make). This inherits
    # the schema-blind-hallucination protection the removed ARS block provided.
    # #1455 removed the prior ``if valid_filter:`` gate (the unfiltered browse
    # used to stay compact, an asymmetry): the page is limit-capped and the
    # default limit dropped 20→10, so a uniformly-enriched page is token-bounded
    # (≈ the old narrowed@20 worst case) while making the unfiltered browse just
    # as actionable as a narrowed one.
    from reyn.tools import get_default_registry
    _registry = get_default_registry()
    enriched: list[dict[str, Any]] = []
    for it in page:
        one = _describe_one(it["qualified_name"], ctx, _registry)
        enriched.append({**it, **one} if one is not None else it)
    page = enriched

    response: dict[str, Any] = {"items": page, "total": total}
    if _should_inject_hidden_state_hint(ctx.router_state):
        response["hint"] = _HIDDEN_STATE_HINT
    return response


async def _handle_search_actions(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """search_actions handler — Phase 2 step 1 semantic search.

    Per §D13 / §D14, semantic search routes through an
    ``ActionEmbeddingIndex`` populated from the catalog enumeration.
    RouterLoop builds the index on first turn when the operator has
    configured ``action_retrieval.embedding_class`` and binds the
    index + provider + model class into the ``RouterCallerState``.

    Response shape per §D11:
        ``{items: [{qualified_name, short_description, score}, ...]}``

    Graceful degradation:
      - ``ctx.router_state`` absent → empty result
      - ``action_embedding_index`` absent → empty result
      - index ``is_ready()`` False (= still building / never built)
        → empty result
      - missing ``query`` argument → §D12 missing-arg error
      - provider / model class missing → empty result

    Concrete: when the visibility gate (build_tools §D14) is honored,
    the handler is only invoked when the index is configured.  The
    None-checks above are defense-in-depth for narrow callers (= plan
    steps / test sites) that bypass the gate.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {
            "error": "missing required argument 'query'",
            "reason": (
                "search_actions requires a non-empty string `query` "
                "describing what to search for."
            ),
            "hint": (
                "Call search_actions(query='...') with a natural-language "
                "description of the action you need."
            ),
        }

    rs = ctx.router_state
    if rs is None:
        return {"items": [], "total": 0}

    idx = rs.action_embedding_index
    provider = rs.embedding_provider
    model_class = rs.embedding_model_class
    if idx is None or provider is None or not model_class:
        return {"items": [], "total": 0}

    # Optional category restriction (§D14 schema), default = all.
    # #934: validate up-front; stale-enum entries surface as an explicit
    # error envelope rather than silently dropping.
    raw_filter = args.get("category") or []
    valid_filter, err = _validate_category_filter(raw_filter)
    if err is not None:
        return err
    category_set = set(valid_filter) if valid_filter else None

    limit = args.get("limit", 10)
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 10

    # Over-fetch when filtering by category so we still return up to
    # ``limit`` after the post-filter cut.
    raw_top_k = limit * len(CATEGORIES) if category_set else limit
    results = await idx.query(query, provider, model_class, top_k=raw_top_k)

    if category_set:
        from reyn.tools.universal_catalog import split_qualified_name
        filtered: list[dict[str, Any]] = []
        for it in results:
            try:
                cat, _ = split_qualified_name(str(it.get("qualified_name", "")))
            except ValueError:
                continue
            if cat in category_set:
                filtered.append(it)
            if len(filtered) >= limit:
                break
        results = filtered[:limit]
    else:
        results = results[:limit]

    return {"items": results, "total": len(results)}


def _describe_one(
    qualified_name: str, ctx: ToolContext, registry: Any,
) -> "dict[str, Any] | None":
    """Resolve ``{description, input_schema}`` for one qualified action.

    The shared selection-grade core of ``describe_action`` AND ``list_actions``'
    enriched items, so the two return the SAME description + schema for a given
    action BY CONSTRUCTION (list ≡ describe). Returns ``None`` when the name
    doesn't resolve or has no registry target (the caller skips / errors as it
    sees fit). Intentionally returns ONLY description + input_schema — the
    describe_action metadata block and the B41 post-call directive stay in
    ``describe_action`` and are not carried into ``list_actions`` items.

    Per-resource description/schema (``_resource_description`` /
    ``_resource_input_schema``) win over the dispatcher target's generic
    fields; the target ``.description`` / ``.parameters`` are the fallback for
    operation-category actions (file__edit, exec__sandboxed_exec, …).
    """
    from reyn.tools.universal_dispatch import (
        UnknownActionError,
        resolve_describe_action,
    )

    try:
        resolved = resolve_describe_action(qualified_name)
    except UnknownActionError:
        return None
    target = registry.lookup(resolved.target_tool_name)
    if target is None:
        return None

    resource_schema = _resource_input_schema(qualified_name, ctx, registry)
    input_schema = (
        resource_schema if resource_schema is not None
        else dict(target.parameters)
    )
    resource_desc = _resource_description(qualified_name, ctx, registry)
    description = (
        resource_desc if resource_desc is not None
        else target.description
    )
    return {"description": description, "input_schema": input_schema}


def catalog_entries(ctx: ToolContext) -> list[dict[str, Any]]:
    """Every usable action as a FLAT generic tool-schema dict
    ``{name, description, parameters}`` — the #1593 ``SchemeOps.catalog_entries``
    projection a scheme presents however it likes (enumerate-all flat, CodeAct
    code-API, retrieval subset). Exposes the **actions**, not the 13-category
    hierarchy (the P7 boundary: the catalog structure stays universal-internal;
    what crosses is a flat action list any scheme can render).

    Single-source: built from the SAME ``_enumerate_category`` (availability-gated
    on ``ctx.router_state``) + ``_describe_one`` (description + input_schema) that
    ``list_actions`` / ``describe_action`` use, so all agree BY CONSTRUCTION
    (#1455 list ≡ describe), no logic fork.

    Schema-completeness bar (CodeAct is the strictest consumer — it renders each
    entry as a Python function signature, so a missing schema = an unusable
    code-API): every returned entry carries a non-None ``parameters`` object —
    unresolvable actions are dropped, and an action with no declared input schema
    gets the empty-but-valid ``{"type": "object", "properties": {}}`` (a valid
    no-arg signature) rather than ``None``.

    Deterministic ``name`` sort (stable ``tools=`` ordering → replay-fixture
    stability). **Pass a ``ToolContext`` with ``router_state`` populated** or the
    resource categories (skills / agents / mcp_servers / …) enumerate empty and
    only static categories survive (the "usable this session" semantics).
    """
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    entries: list[dict[str, Any]] = []
    for category in CATEGORIES:
        for item in _enumerate_category(category, ctx):
            qualified_name = item["qualified_name"]
            one = _describe_one(qualified_name, ctx, registry)
            if one is None:
                # Unresolvable action (no registry target) — not a usable entry.
                continue
            parameters = one.get("input_schema")
            if not isinstance(parameters, dict):
                # Completeness bar: a valid no-arg signature, never None.
                parameters = {"type": "object", "properties": {}}
            entries.append({
                "name": qualified_name,
                "description": one.get("description") or "",
                "parameters": parameters,
            })
    entries.sort(key=lambda entry: entry["name"])
    return entries


async def _handle_describe_action(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """describe_action handler — return target's description + input_schema.

    Per FP-0034 §D11, returns ``{long_description?, input_schema,
    metadata?}``. PR-3a mapped this to the target ToolDefinition's
    ``.parameters`` directly — which is correct for operation-category
    actions (web__fetch / file__read / …) whose target IS the action.

    For resource-category actions (``skill__X``, ``agent.peer__X``,
    ``mcp.tool__X.Y``, ``mcp.server__X``, ``rag_corpus__X``) the target
    is a generic dispatcher (``invoke_skill`` / ``delegate_to_agent`` /
    ``call_mcp_tool`` / …) whose ``.parameters`` is the dispatcher's
    own args shape, NOT the resource's actual input schema. D2-full
    extends the handler to look up the per-resource schema via
    ``ctx.router_state`` so the LLM gets actionable structure instead
    of an opaque ``{name, input}`` envelope (or worse — empty stub).

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

    # D2-full / B42-NF-W7-1: description + input_schema come from the shared
    # _describe_one core (per-resource fields win over the dispatcher target's
    # generic ones, falling back to target.parameters/.description for
    # operation categories), so describe_action and list_actions' enriched
    # items agree BY CONSTRUCTION. ``one`` is non-None here — the resolve +
    # registry lookup above already succeeded, so _describe_one's same
    # resolution does too.
    one = _describe_one(qualified_name, ctx, registry) or {}

    return {
        "qualified_name": qualified_name,
        "description": one.get("description"),
        "input_schema": one.get("input_schema"),
        "metadata": {
            "target_tool_name": resolved.target_tool_name,
            "category": target.category,
            "purity": target.purity,
        },
        # B41-NF-W7-1: post-call directive appended outside the JSON
        # tool-result by the router-loop message-construction layer so the
        # LLM sees a textual instruction after the metadata. Without this,
        # follow-up queries that call describe_action (e.g. "tell me more
        # about the simplest one") trigger 10/10 empty-stop in N=10 replay
        # because the LLM treats the structured metadata as a self-contained
        # answer. Variant F patch test (= directive appended outside the
        # JSON) yielded 1/10 empty-stop on the same trace.
        "_post_text": (
            "The action metadata is above. The user is waiting for your "
            "natural-language reply explaining this action. Write the reply now."
        ),
    }


def _drop_field_from_schema(params: Mapping[str, Any], field_name: str) -> dict:
    """Return a copy of a JSON schema with ``field_name`` removed from
    ``properties`` and ``required``.

    Used to strip curried fields from a dispatcher's parameters before
    exposing them as the resource's input schema (e.g. ``delegate_to_agent``
    carries ``to`` which is curried from ``agent.peer__<name>``).
    """
    out = dict(params)
    props = dict(out.get("properties") or {})
    props.pop(field_name, None)
    out["properties"] = props
    req = [r for r in (out.get("required") or []) if r != field_name]
    out["required"] = req
    return out


def _find_mcp_tool(entry_name: str, rs: Any) -> "Mapping | None":
    """Find the cached MCP tool dict for a ``mcp__<server>__<tool>`` action's
    entry_name (= ``"<server>__<tool>"``), or ``None``.

    #1647: returns None for a static mcp verb (entry_name has no ``__``, e.g.
    ``call_tool`` / ``list_tools``), for an unknown server/tool, or when the
    FP-0037 snapshot (``rs.mcp_servers[*].tools``) isn't warm — so the describe
    helpers fall back to the dispatcher target for verbs while surfacing the real
    per-tool schema/description for tools. Splits on the FIRST ``__`` (server =
    first segment; the same convention ``mcp_call_tool`` uses), so a tool name
    may itself contain ``__``."""
    if rs is None or "__" not in entry_name:
        return None
    server_name, tool_name = entry_name.split("__", 1)
    if not server_name or not tool_name:
        return None
    for server in (getattr(rs, "mcp_servers", None) or []):
        if not isinstance(server, Mapping) or server.get("name") != server_name:
            continue
        for t in (server.get("tools") or []):
            if isinstance(t, Mapping) and t.get("name") == tool_name:
                return t
    return None


def _resource_input_schema(
    qualified_name: str,
    ctx: ToolContext,
    registry: Any,
) -> "dict | None":
    """Return the per-resource input schema for a resource-category action,
    or ``None`` for operation categories (= caller falls back to target's
    parameters).

    Covered:
      - ``skill__<name>`` — uses ``ctx.router_state.host.list_available_skills()``
        which carries ``input_schema`` after FP-0034 D2-full.
      - ``agent.peer__<name>`` — ``delegate_to_agent`` parameters minus ``to``.
      - ``mcp.server__<name>`` — empty object (``list_mcp_tools`` takes
        only the curried ``server`` arg).
      - ``rag_corpus__<name>`` — ``recall`` parameters minus ``sources``.
      - ``mcp__<server>__<tool>`` — scans ``ctx.router_state.mcp_servers``
        for the tool's declared ``inputSchema`` (#1647 per-tool action; static
        mcp verbs fall through to the verb's parameters).

    Returns ``None`` when the category isn't a resource category, or when
    the per-resource metadata isn't reachable (= test sites with stub
    router_state, plan-step host without mcp_servers, …).
    """
    rs = getattr(ctx, "router_state", None)

    try:
        category, entry_name = split_qualified_name(qualified_name)
    except ValueError:
        return None

    if category == "skill":
        host = getattr(rs, "host", None) if rs is not None else None
        if host is None or not hasattr(host, "list_available_skills"):
            return None
        try:
            for skill in host.list_available_skills():
                if not isinstance(skill, Mapping):
                    continue
                if skill.get("name") == entry_name and "input_schema" in skill:
                    return dict(skill["input_schema"])
        except Exception:
            return None
        return None

    if category == "rag_corpus":
        tool = registry.lookup("recall")
        if tool is None:
            return None
        return _drop_field_from_schema(tool.parameters, "sources")

    if category == "mcp":
        # #1647: a per-tool action mcp__<server>__<tool> describes with the MCP
        # tool's OWN declared inputSchema (so the LLM constructs args directly,
        # one level — no generic call_mcp_tool {tool, tool_args} envelope). Static
        # mcp verbs (entry_name w/o "__") → None → caller falls back to the verb's
        # parameters.
        t = _find_mcp_tool(entry_name, rs)
        if t is not None and isinstance(t.get("inputSchema"), Mapping):
            return dict(t["inputSchema"])
        return None

    # memory_entry__X and any other category: pre-existing dispatch shape
    # mismatch (memory_entry's transform sends {name} but read_memory_body
    # wants {layer, slug}); fall back to target.parameters so the LLM at
    # least sees the dispatcher's shape and can recover via list_actions.
    return None


def _resource_description(
    qualified_name: str,
    ctx: ToolContext,
    registry: Any,
) -> "str | None":
    """Return the per-resource description for a resource-category action,
    or ``None`` for operation categories (= caller falls back to
    ``target.description``, which is the correct text for operation
    categories whose target IS the action).

    Mirrors ``_resource_input_schema`` for the description field. B42-NF-W7-1
    fix: without per-resource descriptions, describe_action on a resource
    returns the dispatcher's generic instruction text — uninformative for
    the LLM trying to narrate "tell me more about <resource>".

    Covered (= categories with per-resource description metadata on the
    host side):
      - ``skill__<name>`` — pulls ``description`` from
        ``ctx.router_state.host.list_available_skills()``.
      - ``agent.peer__<name>`` — pulls ``description`` (or ``role``
        fallback) from ``ctx.router_state.host.list_available_agents()``.
      - ``mcp__<server>__<tool>`` — pulls ``description`` from the tool's
        MCP-server entry in ``ctx.router_state.mcp_servers`` (#1647 per-tool
        action; static mcp verbs fall through to the verb's description).
      - ``mcp.server__<name>`` — pulls server-level ``description`` from
        ``ctx.router_state.mcp_servers``.

    Falls through to ``target.description`` (= caller default) for:
      - ``rag_corpus__<name>`` — no per-corpus description metadata
        surface today; caller falls back to the ``recall`` tool
        description (= acceptable generic context for LLM narration).
      - ``memory_entry__<name>`` — memory entries don't carry description
        fields; caller falls back to ``read_memory_body`` description.
      - Operation categories (= ``file__/web__/exec__/...``): the target
        ToolDefinition IS the action, so ``target.description`` is
        already the correct per-action text.

    Coverage delta vs ``_resource_input_schema``: that helper covers
    5 categories (skill / agent.peer / mcp.server / **rag_corpus** /
    mcp.tool). This helper covers 4 (= same set minus rag_corpus) because
    the host-side per-corpus description surface doesn't exist; if a
    ``list_available_corpora()`` surface is added later, this helper
    should grow a matching branch.

    Returns ``None`` for unrecognised categories or when per-resource
    metadata isn't reachable (= test sites with stub router_state, etc.).
    """
    rs = getattr(ctx, "router_state", None)

    try:
        category, entry_name = split_qualified_name(qualified_name)
    except ValueError:
        return None

    if category == "skill":
        host = getattr(rs, "host", None) if rs is not None else None
        if host is None or not hasattr(host, "list_available_skills"):
            return None
        try:
            for skill in host.list_available_skills():
                if not isinstance(skill, Mapping):
                    continue
                if skill.get("name") == entry_name:
                    desc = skill.get("description")
                    return str(desc) if desc else None
        except Exception:
            return None
        return None

    if category == "mcp":
        # #1647: per-tool action mcp__<server>__<tool> — the MCP tool's own
        # description. Static verbs → None → caller falls back to the verb's text.
        t = _find_mcp_tool(entry_name, rs)
        if t is not None:
            desc = t.get("description")
            return str(desc) if desc else None
        return None

    # rag_corpus / memory_entry / unknown — fall through to target.description
    return None


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
    agents / mcp.tool / mcp.server / memory_entry) so the suggestion
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
    # Assertive WHAT/WHEN/WHEN NOT/PREFERRED OVER description constants (Lever C).
    "_LIST_ACTIONS_DESCRIPTION",
    "_SEARCH_ACTIONS_DESCRIPTION",
    "_DESCRIBE_ACTION_DESCRIPTION",
    "_INVOKE_ACTION_DESCRIPTION",
    "split_qualified_name",
    "build_qualified_name",
    "is_valid_qualified_name",
    "is_search_available",
    "is_exec_available",
    "visible_categories",
]
