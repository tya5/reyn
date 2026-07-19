"""Universal catalog wrappers — FP-0034 Phase 1 foundation + PR-3a wiring.

This module defines the 4 universal wrapper ToolDefinitions
(``list_actions`` / ``search_actions`` / ``describe_action`` /
``invoke_action``) plus the qualified-name parser/builder and the
canonical category enum that FP-0034 establishes.

Per FP-0034 §D1, the universal catalog replaces the per-category
discover ops (= ``list_mcp_tools`` / ``list_memory``
etc.) with 4 wrappers that cover every category uniformly. Per
§D18, qualified names use ``<category>__<entry_name>`` format with
``__`` (double underscore) as the separator.

**#3026 — the enumerated action set is a constant.** Every category here
enumerates a FIXED set of verbs. No category mints an action from operator
data, so the number of tools the LLM is sent does not depend on how many
memories, corpora, MCP tools or pipelines the operator has accumulated. This
is the invariant this module exists to hold: every name it emits comes from
``universal_dispatch._OPERATION_RULES``, a closed table of full literal names.

``universal_dispatch._RESOURCE_RULES`` still RESOLVES author-time resource
names (``pipeline__<name>``, ``tool: mcp__echo__ping`` in a pipeline DSL) —
that table is deliberately NOT read here. Resolving a name the caller already
typed costs zero tools; enumerating one costs a tool per resource. Keep the
two apart: reading ``_RESOURCE_RULES`` from an enumerator is precisely the
#1647 regression.

The four collapses that got here — #879 (mcp.server/mcp.tool), #909
(agent.peer), and #3026 (memory_entry, rag_corpus, plus the dynamic
``mcp__<server>__<tool>`` and ``pipeline__<name>`` entries) — all applied one
rule: a resource is an ARGUMENT to a verb, never a tool of its own. Where
collapsing removed the only surface that NAMED a resource, #3026 added a
constant-count discovery verb rather than accepting the loss
(``rag_operation__list_sources``, ``pipeline__list``, and the
``memory_operation__list`` / ``__read`` routes).

**#879 → #1647 is the cautionary tale.** #879 collapsed the mcp resource
categories; #1647 re-added a per-tool action for every MCP tool, citing (a)
``call_mcp_tool``'s double-``args`` foot-gun — which #1646 had fixed two days
earlier by renaming the inner param to ``tool_args`` — and (b) the need to
show each tool's real ``inputSchema``, which #879 had ALREADY solved by
shipping ``inputSchema`` verbatim in ``list_mcp_tools``' result, explicitly
so no ``describe_mcp_tool`` round-trip is needed (see the docstring in
``tools/mcp.py``). Its design note says it mirrors ``skill__<name>`` — a
category that has never existed. Before re-introducing per-resource actions,
check whether the motivating gap is still open: twice now it was not.

PR-1 (landed): type surface only — 4 ToolDefinitions with stub
handlers, qualified-name parse / build / validate, 14-category enum,
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

from reyn.tools.descriptions import catalog as _catalog_descriptions
from reyn.tools.descriptions import discovery
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Lazy-imported at function-body level to break the circular dependency
# with universal_dispatch.py (which imports CATEGORIES + split_qualified_name
# from this module). The handlers below import the dispatch symbols inside
# their function bodies; this typing-time alias is for type checkers only.
if TYPE_CHECKING:
    from reyn.tools.universal_dispatch import UnknownActionError


# ── Canonical 14-category enum (FP-0034 §D18 master taxonomy) ──────────────
#
# Order matches the master table in FP-0034 §D18 so reviewers reading the
# design doc and the code see the same shape. ``exec`` ships last because
# it is the only category with hard sandbox-backend gating (= D14 / D14-ext).
CATEGORIES: Final[tuple[str, ...]] = (
    # Phase 1 follow-up (2026-05-25): collapsed ``agent.peer`` resource
    # category into ``multi_agent`` verb category (= list_peers /
    # describe_peer / delegate). Same shape rationale as #879 mcp
    # collapse — resource entries (agent names) → verb actions whose
    # args carry the agent name explicitly.
    "multi_agent",
    # Issue #879: collapsed the previous mcp.server / mcp.tool /
    # mcp.operation sub-categories + prior mcp search/install actions
    # into a single ``mcp`` category. 2026-05-25: install surface
    # further split along the source axis into 3 verbs (registry /
    # package / local). Full verb set: mcp__search_registry,
    # mcp__install_registry, mcp__install_package, mcp__install_local,
    # mcp__list_servers, mcp__list_tools, mcp__call_tool,
    # mcp__drop_server. See universal_dispatch._OPERATION_RULES.
    "mcp",
    "file",
    "web",
    # #3026: ``memory_entry`` / ``rag_corpus`` removed. They were RESOURCE
    # categories — one action per stored memory / indexed corpus — so the LLM's
    # tools= payload scaled with what the operator had accumulated. Their verb
    # counterparts below now carry the resource id as an ARGUMENT
    # (memory_operation__read{layer,slug} / rag_operation__list_sources +
    # __semantic_search{sources}). Same shape rationale as the #879 mcp collapse
    # and the #909 agent.peer collapse.
    "memory_operation",
    "reyn_repo",
    "rag_operation",
    "exec",
    "task",  # #1953 dynamic-wire: task.* control-IR ops as invoke_action targets
    # #2548 PR-C: skill management ops (install / list). Skills are the
    # already-correct shape and always were: there is no ``skill__`` resource
    # category — despite what several comments in this repo used to claim, and
    # what #1647 said it was mirroring — so skills have never added a tool per
    # skill. #2971 added the ``skill_management__list`` DISCOVERY verb, which is
    # the same move #3026 makes for corpora and pipelines.
    "skill_management",
    # IS-1/IS-2/IS-4 (docs/proposals/reyn-pipeline-v0.9-design-resolutions.md
    # R6): pipeline launch verbs. ``pipeline__run`` = run_pipeline (sync,
    # REGISTERED-only); ``pipeline__run_async`` = run_pipeline_async (IS-2:
    # background launch in a crash-recoverable driver-session);
    # ``pipeline__run_inline`` / ``pipeline__run_inline_async`` (IS-4) = the
    # ad-hoc INLINE launches of an agent-GENERATED DSL definition, gated by a
    # static-analysis pass before spawn.
    "pipeline",
    # pipeline management ops (install_local / install_source) — the management
    # plane, mirroring ``skill_management``. (#3026 removed the per-registered-
    # pipeline ``pipeline__<name>`` dynamic actions; ``pipeline`` is now launch
    # verbs + ``pipeline__list`` only.)
    "pipeline_management",
    # proposal 0060 Phase 1 Layer A (A8): presentation management ops (install).
    # Single verb (no source/git-fetch counterpart — a blueprint is inline
    # declarative data). Management plane — mirrors ``skill_management`` /
    # ``pipeline_management``.
    "presentation_management",
    # ADR 0064 P2: plugin management ops (install / uninstall). #3083: this
    # category was ADDED to ``_OPERATION_RULES`` (dispatch-wired) when the P2
    # verbs landed, but never added HERE — the exact #2032-class gap the
    # comments above this tuple already document for skill_management /
    # pipeline_management / presentation_management. Registered +
    # dispatchable but absent from CATEGORIES means every enumerate-all /
    # retrieval / codeact scheme's ``tools=`` payload never carried
    # plugin_management__install/__uninstall, so the LLM could never
    # discover — let alone call — them. See
    # ``test_categories_covers_every_dispatch_wired_category`` in
    # ``tests/test_universal_catalog.py`` for the routing-table-derived
    # gate that now guards against a category being dispatch-wired without
    # a matching CATEGORIES entry.
    "plugin_management",
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
        ``mcp__call_tool``            → ("mcp", "call_tool")
        ``rag_operation__list_sources`` → ("rag_operation", "list_sources")
        ``memory_operation__read``    → ("memory_operation", "read")

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


# ── provider tool-name normalization (#1989) ───────────────────────────────

# Known LLM function-calling namespace prefixes a model may echo onto a tool
# name. Gemini wraps tools in a ``default_api`` namespace and a weak model
# sometimes emits ``default_api.<tool>`` (e.g. ``default_api.invoke_action`` /
# ``default_api.web__search``) — both as a function-call name and, observed in
# #1989, as a string value inside a ``plan``'s step ``tools``. Stripping a
# leading one is SAFE for EVERY provider: reyn tool names never contain a ``.``
# — qualified names use ``__`` (``_NAME_SEPARATOR``) and bare verbs use single
# underscores — so a dot-delimited ``<namespace>.`` prefix can never be part of a
# legit reyn name. Extending the set (e.g. OpenAI ``functions.``) is a one-line add.
_PROVIDER_TOOL_NAMESPACES: tuple[str, ...] = ("default_api.",)


def strip_provider_tool_namespace(name: str) -> str:
    """Strip a leading provider function-calling namespace prefix from a tool
    name (#1989). A no-op for a name without a known prefix (so it is safe to
    apply unconditionally). Safe across providers because reyn names are
    dot-free, so a ``<namespace>.`` prefix is never part of a legit name."""
    for ns in _PROVIDER_TOOL_NAMESPACES:
        if name.startswith(ns):
            return name[len(ns):]
    return name


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


# Reviewable in src/reyn/tools/descriptions/discovery.py (Phase 1 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_LIST_ACTIONS_DESCRIPTION = discovery.list_actions.text


_LIST_ACTIONS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "array",
            "items": {"type": "string", "enum": list(CATEGORIES)},
            # The reviewable ``.text`` is the STATIC prefix ending in
            # "Categories: "; the live CATEGORIES list is appended here so
            # the rendered string stays byte-identical to the pre-migration
            # literal (see discovery.PARAMS's docstring note on this entry).
            "description": (
                discovery.PARAMS["list_actions"]["category"].text
                + ", ".join(CATEGORIES) + "."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": discovery.PARAMS["list_actions"]["offset"].text,
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 10,
            "description": discovery.PARAMS["list_actions"]["limit"].text,
        },
    },
}


# Reviewable in src/reyn/tools/descriptions/discovery.py (Phase 1 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_SEARCH_ACTIONS_DESCRIPTION = discovery.search_actions.text


_SEARCH_ACTIONS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": discovery.PARAMS["search_actions"]["query"].text,
        },
        "category": {
            "type": "array",
            "items": {"type": "string", "enum": list(CATEGORIES)},
            "description": discovery.PARAMS["search_actions"]["category"].text,
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 10,
            "description": discovery.PARAMS["search_actions"]["limit"].text,
        },
    },
    "required": ["query"],
}


# Reviewable in src/reyn/tools/descriptions/discovery.py (Phase 1 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_DESCRIBE_ACTION_DESCRIPTION = discovery.describe_action.text


_DESCRIBE_ACTION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_name": {
            "type": "string",
            "description": discovery.PARAMS["describe_action"]["action_name"].text,
        },
    },
    "required": ["action_name"],
}


# Relocated to reyn.tools.descriptions.catalog (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_INVOKE_ACTION_DESCRIPTION = _catalog_descriptions.invoke_action.text


_INVOKE_ACTION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_name": {
            "type": "string",
            "description": _catalog_descriptions.PARAMS["invoke_action"]["action_name"].text,
        },
        "args": {
            "type": "object",
            "description": _catalog_descriptions.PARAMS["invoke_action"]["args"].text,
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
            "Provide action_name (qualified, e.g. 'mcp__brave__search') "
            "from list_actions or search_actions output."
        ),
    }


def _enumerate_static_category(category: str) -> list[dict[str, str]]:
    """Enumerate qualified names for a STATIC operation category.

    #3026: EVERY category is a static operation category now — all qualified
    names are declared in ``universal_dispatch._OPERATION_RULES``, and each
    entry's short_description comes from its target ToolDefinition in the
    registry. The former resource categories, which minted names from caller
    state (``ctx.router_state.available_*``) and so scaled the payload with the
    operator's data, are collapsed into verbs.
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
    """Enumerate the qualified names ``category`` offers this session.

    EVERY category resolves to a fixed verb set read out of
    ``universal_dispatch._OPERATION_RULES`` via ``_enumerate_static_category``.
    There is no per-category branch that mints a name from operator data, and
    ``_RESOURCE_RULES`` is deliberately NOT read here (#3026) — that table
    exists so an author-time name a human already typed still RESOLVES, which
    costs no tools; enumerating one costs a tool per resource. This function is
    where the payload invariant lives, so keep it that way: the number of names
    returned must not depend on how many memories / corpora / MCP tools /
    pipelines the operator has accumulated.

    ``ctx.router_state`` is consulted, but only ever to decide whether a FIXED
    verb is AVAILABLE this session — never to invent one:
      - ``excluded_categories`` (#1667) — the caller drops a whole category.
      - ``sandbox_backend`` (D14-ext) — ``exec`` enumerates its single verb only
        when a real backend is configured, and nothing otherwise.

    The output items each carry ``qualified_name`` (= what
    invoke_action / describe_action expects) and ``short_description``
    (= LLM-facing summary, truncated per _MAX_SHORT_DESC).
    """
    rs = ctx.router_state

    # #1667: explicit per-session category exclusion. The task-agent / external-repo
    # eval path (e.g. SWE-bench on /testbed) sets ``excluded_categories`` so a
    # category irrelevant to the task — Reyn's own ``reyn_repo`` self-help surface
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
        "file", "web", "memory_operation", "reyn_repo", "rag_operation",
        "multi_agent",
        # #1953 dynamic-wire: the 12 task.* ops have static qualified names in
        # _OPERATION_RULES (task__create/…) → enumerate them here too. Without
        # this, task ops were DISPATCH-wired (invoke_action) but NOT ENUMERATED
        # → unreachable on the enumerate-all production-default scheme + empty
        # list_actions(task). The single-source enumeration seam (#2032).
        "task",
        # Pre-existing #2032-class gap found + closed while adding
        # pipeline_management: skill_management had a static _OPERATION_RULES
        # entry + dispatch route but was NEVER added to this enumeration list —
        # list_actions(category=["skill_management"]) silently returned empty
        # even though skill_management__install_local/_source were fully
        # dispatchable via invoke_action (the exact "registered + dispatchable
        # but LLM-invisible" bug class, same root cause as #2589/#2621).
        # pipeline_management is added alongside it so the new verbs don't
        # repeat the same gap.
        "skill_management",
        "pipeline_management",
        # proposal 0060 Phase 1 Layer A (A8): same enumeration wiring as
        # skill_management / pipeline_management so
        # list_actions(category=["presentation_management"]) surfaces the
        # install verb (not just dispatchable-but-invisible).
        "presentation_management",
        # #3083: same enumeration wiring for plugin_management — closes the
        # dogfood-witnessed 0/75 gap (plugin_management__install/__uninstall
        # were registered + dispatch-wired but never enumerated).
        "plugin_management",
    ):
        return _enumerate_static_category(category)

    # #3026: the ``mcp`` category is its static verbs and nothing else. #1647
    # additionally emitted one ``mcp__<server>__<tool>`` action per tool on every
    # connected server, so the payload scaled with the operator's MCP surface.
    # Its own commit called that layer "purely a catalog/args ergonomics layer
    # over call_mcp_tool" — zero capability of its own. See the module docstring
    # for why the ergonomics argument no longer holds either.
    if category == "mcp":
        return _enumerate_static_category("mcp")

    if category == "pipeline":
        # #2589: the static launch verbs (pipeline__run / _async / _inline /
        # _inline_async) live in ``_OPERATION_RULES`` but were never enumerated,
        # so a default enumerate-all agent could dispatch them yet never
        # discover them. #3026: this is now the WHOLE of the category — the IS-5
        # per-registered-pipeline ``pipeline__<name>`` entries are gone, because
        # one action per registered pipeline made the payload scale with the
        # operator's pipelines. They cost no capability to remove (each merely
        # curried ``name`` into the ``pipeline__run`` this list already carries);
        # the one thing they did uniquely — NAMING the registered pipelines — is
        # now ``pipeline__list``, a single fixed verb.
        return _enumerate_static_category("pipeline")

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
    # #3026 — the last two resource categories collapsed into their verb
    # counterparts. A model whose catalog snapshot pre-dates the collapse asks
    # for these by name; the redirect lets it self-correct in one turn.
    "memory_entry": "memory_operation",
    "rag_corpus": "rag_operation",
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
    "in this session. To enable it, add to reyn.yaml: "
    "`action_retrieval:\\n  embedding_class: standard` — uses OpenAI "
    "embeddings, requires `OPENAI_API_KEY` (or point `embedding_class` at "
    "another `embedding.classes` entry, e.g. a litellm-fronted proxy for a "
    "local model).\n"
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
    the hint is informational, not blocking; the LLM may surface the
    enable-hint (= configure ``action_retrieval.embedding_class`` in
    reyn.yaml) once during boot, then stop on subsequent turns once
    the index becomes ready.
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

    # FP-0057 #2856 Part A: idx.query() now routes through the shared `embed`
    # op (execute_op) rather than calling ``provider`` directly, so it needs
    # an OpContext — built via the same factory (rs.op_context_factory =
    # host.make_router_op_context) other tool-use ops already thread. The
    # ``provider`` None-check above stays as the D14 configured-signal.
    op_ctx_factory = rs.op_context_factory
    if op_ctx_factory is None:
        return {"items": [], "total": 0}
    op_ctx = op_ctx_factory()

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
    results = await idx.query(query, op_ctx, model_class, top_k=raw_top_k)

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

    #3026: every action's description + schema is now simply its target
    ToolDefinition's, because every action IS a verb whose target is the action.
    The former per-resource override pair (``_resource_description`` /
    ``_resource_input_schema``) existed to paper over resource actions whose
    target was a generic dispatcher — ``rag_corpus__<name>`` showing
    ``semantic_search``'s schema minus the curried ``sources``, and so on. With
    the resource categories collapsed there is no such action left, so the
    override seam is gone rather than kept as an unused hook. ``ctx`` stays in
    the signature: it is what ``list_actions`` / ``catalog_entries`` already
    thread, and removing it would churn both call sites for nothing.
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

    return {
        "description": target.description,
        "input_schema": dict(target.parameters),
    }


def catalog_entries(ctx: ToolContext) -> list[dict[str, Any]]:
    """Every usable action as a FLAT generic tool-schema dict
    ``{name, description, parameters}`` — the #1593 ``SchemeOps.catalog_entries``
    projection a scheme presents however it likes (enumerate-all flat, CodeAct
    code-API, retrieval subset). Exposes the **actions**, not the 14-category
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
    stability). **Pass a ``ToolContext`` with ``router_state`` populated**: no
    category needs it to produce names any more (#3026), but it still gates
    AVAILABILITY — ``excluded_categories`` and ``exec``'s sandbox backend — so a
    None ``router_state`` yields a superset-shaped list that is not the "usable
    this session" set. Note the count does NOT depend on how much the operator
    has accumulated; that invariant is pinned in
    ``tests/test_resource_collapse_invariant_3026.py``.
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

    #3026: that is now the whole story — every enumerated action IS its target,
    so the target's ``.parameters`` is always the right schema. The D2-full
    per-resource override (look the schema up from ``ctx.router_state`` because
    the target was a generic dispatcher) went with the resource categories it
    served; see ``_describe_one``.

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
    (= send_to_agent / op_context_factory / list_memory_fn / etc.)
    reach the target handler as if the caller had invoked it directly.
    This is what makes invoke_action a transparent wrapper rather than
    a separate execution path.

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
    # router_state via the typed sub-object.
    result = await target.handler(resolved.target_args, ctx)
    # FP-0056 PR-F1: tag the RESOLVED target tool name so canonicalization dispatches by the true
    # invoked identity, not the ``invoke_action`` wrapper (which would resolve to the wrapper's own
    # passthrough declaration and hide the target's text body). The chat/pipeline chokepoints strip
    # this before rendering; dispatch()'s outer tag defers to it (setdefault).
    if isinstance(result, dict) and "_canonical_source" not in result:
        result = {**result, "_canonical_source": resolved.target_tool_name}
    return result


def _augment_suggestions(
    exc: "UnknownActionError", ctx: ToolContext,
) -> "UnknownActionError":
    """Re-suggest using router_state-aware candidates when available.

    The PR-2 default suggestion pool is the static catalogue
    (= KNOWN_STATIC_QUALIFIED_NAMES). This re-derives it from the live
    enumeration instead, so a suggestion is availability-aware: a category the
    caller excluded (#1667), or ``exec`` without a sandbox backend, contributes
    nothing here and is never suggested.

    #3026: it no longer WIDENS the pool. It used to add per-resource names
    (memory entries / corpora / MCP tools) that only caller state knew; those
    are collapsed, so enumeration and the static catalogue now describe the
    same action set and this narrows rather than grows. Falls back to the
    original exception unchanged when enumeration yields nothing.
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


from reyn.core.offload.canonical import (  # noqa: E402
    describe_action_to_canonical,
    invoke_action_to_canonical,
    list_actions_to_canonical,
    search_actions_to_canonical,
)

LIST_ACTIONS = ToolDefinition(
    canonical=list_actions_to_canonical,
    name="list_actions",
    router_dispatched=True,
    description=_LIST_ACTIONS_DESCRIPTION,
    parameters=_LIST_ACTIONS_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_list_actions,
    category="discovery",
    purity="read_only",
)


SEARCH_ACTIONS = ToolDefinition(
    canonical=search_actions_to_canonical,
    name="search_actions",
    router_dispatched=True,
    description=_SEARCH_ACTIONS_DESCRIPTION,
    parameters=_SEARCH_ACTIONS_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_search_actions,
    category="discovery",
    purity="read_only",
)


DESCRIBE_ACTION = ToolDefinition(
    canonical=describe_action_to_canonical,
    name="describe_action",
    router_dispatched=True,
    description=_DESCRIBE_ACTION_DESCRIPTION,
    parameters=_DESCRIBE_ACTION_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_describe_action,
    category="discovery",
    purity="read_only",
)


INVOKE_ACTION = ToolDefinition(
    canonical=invoke_action_to_canonical,
    name="invoke_action",
    router_dispatched=True,
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
