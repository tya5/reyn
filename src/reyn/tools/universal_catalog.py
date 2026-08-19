"""Universal catalog wrappers — FP-0034 Phase 1 foundation + PR-3a wiring.

This module defines the 4 universal wrapper ToolDefinitions
(``list_actions`` / ``search_actions`` / ``describe_action`` /
``invoke_action``) plus the canonical category enum that FP-0034
establishes.

Per FP-0034 §D1, the universal catalog replaces the per-category
discover ops (= ``list_mcp_tools`` / ``list_memory``
etc.) with 4 wrappers that cover every category uniformly.

**#3429 — an action has exactly one name: its flat registry tool name.**
§D18 used to give every action a second, ``<category>__<verb>`` *qualified*
spelling, which this module parsed and built. Both spellings reached a
handler, but they reached it differently, and every subsystem keyed on a
tool name had to remember to handle both — 7 of the 11 that exist did not.
The parser (``split_qualified_name`` / ``build_qualified_name`` /
``is_valid_qualified_name``) is gone with the spelling it parsed; a category
is now purely the browsing axis ``list_actions(category=[…])`` exposes, and
``universal_dispatch._CATEGORY_ACTIONS`` is the membership table.

**#3026 — the catalog *enumeration* is constant.** Every category here
enumerates a FIXED set of verbs. No category mints an action from operator
data, so the number of tools the LLM is sent does not depend on how many
memories, corpora, MCP tools or pipelines the operator has accumulated. This
is the invariant this module exists to hold: every name it emits comes from
``universal_dispatch._CATEGORY_ACTIONS``, a closed table of literal names.

#3429 removed the one remaining exception — the author-time resource names
(``pipeline__<name>``, ``tool: mcp__echo__ping`` in a pipeline DSL) that
RESOLVED without being enumerated. They were the qualified spelling in
operator-facing clothes; a step now names the flat tool and passes the
resource id as an ordinary argument. A rule keyed by anything other than a
literal action name re-opens operator-scaled growth (#1647).

The four collapses that got here — #879 (mcp.server/mcp.tool), #909
(agent.peer), and #3026 (memory_entry, rag_corpus, plus the dynamic
per-MCP-tool and per-pipeline entries) — all applied one
rule: a resource is an ARGUMENT to a verb, never a tool of its own. Where
collapsing removed the only surface that NAMED a resource, #3026 added a
constant-count discovery verb rather than accepting the loss
(``pipeline_list`` and the ``list_memory`` / ``read_memory_body`` routes;
the RAG list-sources verb #3026 added the same way was itself
retired in FP-0066 P1b along with the rest of the layer-1 RAG tools).

**#879 → #1647 is the cautionary tale.** #879 collapsed the mcp resource
categories; #1647 re-added a per-tool action for every MCP tool, citing (a)
``call_mcp_tool``'s double-``args`` foot-gun — which #1646 had fixed two days
earlier by renaming the inner param to ``tool_args`` — and (b) the need to
show each tool's real ``inputSchema``, which #879 had ALREADY solved by
shipping ``inputSchema`` verbatim in ``list_mcp_tools``' result, explicitly
so no ``describe_mcp_tool`` round-trip is needed (see the docstring in
``tools/mcp.py``). Its design note says it mirrors a per-skill category that
has never existed. Before re-introducing per-resource actions,
check whether the motivating gap is still open: twice now it was not.

PR-1 (landed): type surface only — 4 ToolDefinitions with stub
handlers, the 12-category enum, D14 visibility-gating helpers.

PR-2 (landed): the action-membership layer — ``universal_dispatch.py`` with
``require_known_action`` / ``action_names_for_category`` /
``suggest_similar_names``.

PR-3a (landed): wire real handlers — list_actions /
describe_action / invoke_action handlers resolve the named action against
the membership table and dispatch it through the unified ToolRegistry.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Mapping

from reyn.tools.descriptions import catalog as _catalog_descriptions
from reyn.tools.descriptions import discovery
from reyn.tools.types import (
    ToolContext,
    ToolDefinition,
    ToolGates,
    ToolResult,
    parameters_for_export,
)

# Lazy-imported at function-body level to break the circular dependency
# with universal_dispatch.py (which imports CATEGORIES from this module).
# The handlers below import the dispatch symbols inside their function
# bodies; this typing-time alias is for type checkers only.
if TYPE_CHECKING:
    from reyn.tools.universal_dispatch import UnknownActionError


# ── Canonical 12-category enum (FP-0034 §D18 master taxonomy) ──────────────
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
    # package / local). Full verb set: mcp_search_registry,
    # mcp_install_registry, mcp_install_package, mcp_install_local,
    # list_mcp_servers, list_mcp_tools, mcp_call_tool,
    # mcp_drop_server. See universal_dispatch._CATEGORY_ACTIONS.
    "mcp",
    "file",
    "web",
    # #3026: ``memory_entry`` / ``rag_corpus`` removed. They were RESOURCE
    # categories — one action per stored memory / indexed corpus — so the LLM's
    # tools= payload scaled with what the operator had accumulated. The
    # memory_operation verb counterpart below now carries the resource id as an
    # ARGUMENT (read_memory_body{layer,slug}). Same shape rationale as the
    # #879 mcp collapse and the #909 agent.peer collapse. FP-0066 P1b: the
    # ``rag_operation`` category itself (semantic_search / drop_source /
    # index_update / list_sources) is retired outright — those were the
    # agent-facing layer-1 in-core RAG tools, a pre-audience-split relic; see
    # docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md §9.
    "memory_operation",
    "reyn_repo",
    "exec",
    # #2548 PR-C: skill management ops (install / list). Skills are the
    # already-correct shape and always were: there is no ``skill__`` resource
    # category — despite what several comments in this repo used to claim, and
    # what #1647 said it was mirroring — so skills have never added a tool per
    # skill. #2971 added the ``skill_list`` DISCOVERY verb, which is
    # the same move #3026 makes for corpora and pipelines.
    "skill_management",
    # Proposal 0067 P7 (#3978): the unified pipeline launch verb —
    # ``run_pipeline``, ``collect="attached"|"async"`` (was 4 separate
    # names: sync/async x registered-name/ad-hoc-inline-DSL, per
    # docs/proposals/reyn-pipeline-v0.9-design-resolutions.md R6 IS-1/IS-2/
    # IS-4 — retired with 0 aliases, architect ruling). An ad-hoc
    # ``definition=`` DSL string is still gated by the same
    # static-analysis pass before spawn.
    "pipeline",
    # pipeline management ops (install_local / install_source) — the management
    # plane, mirroring ``skill_management``. (#3026 removed the per-registered-
    # pipeline dynamic actions; ``pipeline`` is now launch
    # verbs + ``pipeline_list`` only.)
    "pipeline_management",
    # proposal 0067 P4 (#3978): describe_task / list_tasks / cancel_task —
    # read/act against a currently-running async task's settle-path handle.
    "task",
    # proposal 0060 Phase 1 Layer A (A8): presentation management ops (install).
    # Single verb (no source/git-fetch counterpart — a blueprint is inline
    # declarative data). Management plane — mirrors ``skill_management`` /
    # ``pipeline_management``.
    "presentation_management",
    # ADR 0064 P2: plugin management ops (install / uninstall). #3083: this
    # category was ADDED to the action-membership table (dispatch-wired) when
    # the P2 verbs landed, but never added HERE — the exact #2032-class gap the
    # comments above this tuple already document for skill_management /
    # pipeline_management / presentation_management. Registered +
    # dispatchable but absent from CATEGORIES means every enumerate-all /
    # retrieval / codeact scheme's ``tools=`` payload never carried
    # plugin_install / plugin_uninstall, so the LLM could never
    # discover — let alone call — them. See
    # ``test_categories_covers_every_dispatch_wired_category`` in
    # ``tests/tools/test_universal_catalog.py`` for the routing-table-derived
    # gate that now guards against a category being dispatch-wired without
    # a matching CATEGORIES entry.
    "plugin_management",
    # FP-0066 P3c (#3247 "P3 設計 firm" §3): the ``knowledge`` category —
    # semantic search over the operator's own skill/memory/repo knowledge
    # (``search_knowledge``). Single-entry
    # category, same runtime-gated shape as ``exec`` (visible only when
    # ``embedding.enabled: true`` — see ``_enumerate_category``'s
    # ``"knowledge"`` branch, which shares ``is_search_available`` with
    # ``search_actions``'s own visibility gate rather than re-deriving the
    # embedding-config check a second time).
    "knowledge",
    # #3465: FP-0057 Phase 1 ``embed`` — the raw, USER-FACING batch
    # text->vector primitive (distinct from ``knowledge``'s search-over-own-
    # content axis). Registered + dispatch-wired but missing from CATEGORIES
    # until now, the same #3083-class gap this tuple's comments already
    # document for skill_management / pipeline_management /
    # presentation_management / plugin_management. Always visible — no
    # runtime gate like ``exec``/``knowledge``.
    "embedding",
    # #3465: the hooks management/self-expansion plane — ``emit_hook_event``
    # (publish an LLM-authored hook-event onto this session's own HookBus)
    # and ``hooks_add`` (the agent adds a push hook to its own runtime
    # layer). Both ToolDefinitions already declared ``category="hooks"``;
    # this closes the same "registered + dispatchable but catalog-invisible"
    # gap. Always visible.
    "hooks",
)


# ── provider tool-name normalization (#1989) ───────────────────────────────

# Known LLM function-calling namespace prefixes a model may echo onto a tool
# name. Gemini wraps tools in a ``default_api`` namespace and a weak model
# sometimes emits ``default_api.<tool>`` (e.g. ``default_api.invoke_action`` /
# ``default_api.web_search``) — both as a function-call name and, observed in
# #1989, as a string value inside a ``plan``'s step ``tools``. Stripping a
# leading one is SAFE for EVERY provider: reyn tool names never contain a ``.``
# — they are single-underscore-joined verbs — so a dot-delimited
# ``<namespace>.`` prefix can never be part of a legit reyn name. Extending the
# set (e.g. OpenAI ``functions.``) is a one-line add.
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


def is_search_available(*, embedding_enabled: bool) -> bool:
    """Return True iff ``search_actions`` should be exposed to the LLM.

    Per FP-0034 §D14 / FP-0066 §7, ``search_actions`` is only visible when
    the operator has opted into the embedding-backed semantic-discovery
    layer (``embedding.enabled: true``). Clean-break replacement for the
    retired ``action_retrieval.embedding_class`` gate (which conflated
    on/off with which embedding class to use) — the embedding CLASS itself
    (``embedding.default_class`` / a dangling ``embedding.classes``
    reference) is validated eagerly by ``_build_embedding_config`` at
    config-load time (raises there), so by the time this predicate runs,
    ``embedding_enabled=True`` already implies a resolvable class — no
    membership check is needed here.
    """
    return bool(embedding_enabled)


def is_exec_isolated(*, sandbox_backend: str | None) -> bool:
    """Return True iff ``exec`` runs under a real sandbox backend (isolation
    actually applies) — False when ``sandbox_backend`` is ``None``/``"noop"``.

    #4932 (owner ruling, 2026-08-19, reverses FP-0034 §D14-ext): this
    predicate is DISCLOSURE-ONLY now — it composes the ``exec`` tool's
    per-request isolation notice (``_enumerate_category``'s and
    ``_describe_one``'s ``"exec"`` special-cases), never a VISIBILITY
    decision. D14-ext's original shape (``is_exec_available`` — hide the
    category entirely when noop) borrowed D14's "hide a functionally-dead
    tool" pattern (``search_actions`` with no embedding index configured,
    see ``is_search_available``) and misapplied it to a SECURITY-posture
    question: ``exec`` still WORKS under ``noop``, just without OS-level
    isolation — hiding it produced silent, unpredictable capability loss
    (an operator who switched to ``noop`` for an unrelated reason, e.g.
    #4932's own repro: probing Keychain reachability, lost ``exec``
    entirely with no error, no notice) instead of a disclosed tradeoff.
    Owner ruling (#4932, verbatim): "UX and predictability outrank
    security; security should be opt-in [via a real sandbox backend], not
    silently enforced by hiding a working tool." See
    ``visible_categories`` for the visibility-side reversal.
    """
    if not sandbox_backend:
        return False
    return sandbox_backend != "noop"


# #4932: the notice appended to `exec`'s description (both the
# `_enumerate_category` short form and `_describe_one`'s full form) when
# `is_exec_isolated` is False — the disclosure that REPLACES hiding the
# category. Static text (not backend-name-specific): the LLM needs to know
# isolation is absent, not which specific backend string produced that
# state (`None` vs `"noop"` carry the same operational meaning here).
_EXEC_NO_ISOLATION_NOTICE: Final[str] = (
    " In THIS environment, no sandbox isolation is applied (no sandbox "
    "backend is configured, or it is set to \"noop\") — the command runs "
    "with the operator's own OS-level permissions, unconfined."
)


def visible_categories() -> tuple[str, ...]:
    """Return the categories that should be visible given the current env.

    #4932 (owner ruling, 2026-08-19): ALL categories are always visible —
    ``exec`` no longer has a category-level visibility gate (it used to
    drop out entirely when ``sandbox_backend`` was ``"noop"``/``None``,
    FP-0034 §D14-ext; see ``is_exec_isolated``'s own docstring for why
    that was reversed). This function keeps its own identity (a named,
    testable "what's visible" surface, referenced from
    ``docs/deep-dives/research/fp-0035-permission-communication-analysis.md``)
    even though it is now a constant — a future category-level gate has
    a place to attach without re-inventing this function's name/shape.
    """
    return CATEGORIES


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
            "Provide action_name (e.g. 'web_search') "
            "from list_actions or search_actions output."
        ),
    }


def _enumerate_static_category(category: str) -> list[dict[str, str]]:
    """Enumerate the action names a STATIC operation category offers.

    #3026: EVERY category is a static operation category now — all action
    names are declared in ``universal_dispatch._CATEGORY_ACTIONS``, and each
    entry's short_description comes from its ToolDefinition in the registry.
    The former resource categories, which minted names from caller state
    (``ctx.router_state.available_*``) and so scaled the payload with the
    operator's data, are collapsed into verbs.
    """
    # Lazy imports to avoid circular dependency (universal_dispatch imports
    # CATEGORIES from THIS module).
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import action_names_for_category

    registry = get_default_registry()
    out: list[dict[str, str]] = []
    for action_name in action_names_for_category(category):
        target = registry.lookup(action_name)
        short = _truncate_short_description(
            target.description if target is not None else "",
        )
        out.append({
            "action_name": action_name,
            "short_description": short,
        })
    return out


def _enumerate_category(category: str, ctx: ToolContext) -> list[dict[str, str]]:
    """Enumerate the action names ``category`` offers this session.

    EVERY category resolves to a fixed verb set read out of
    ``universal_dispatch._CATEGORY_ACTIONS`` via ``_enumerate_static_category``.
    There is no per-category branch that mints a name from operator data
    (#3026). This function is
    where the payload invariant lives, so keep it that way: the number of names
    returned must not depend on how many memories / corpora / MCP tools /
    pipelines the operator has accumulated.

    ``ctx.router_state`` is consulted, but only ever to decide whether a FIXED
    verb is AVAILABLE this session — never to invent one:
      - ``excluded_categories`` (#1667) — the caller drops a whole category.
      - ``sandbox_backend`` — #4932 (owner ruling, 2026-08-19): no longer an
        availability gate for ``exec`` (see ``is_exec_isolated``'s own
        docstring for the reversal). Still consulted here, but only to
        compose ``exec``'s isolation-disclosure text — the category itself
        is always enumerated.

    The output items each carry ``action_name`` (= what
    invoke_action / describe_action expects) and ``short_description``
    (= LLM-facing summary, truncated per _MAX_SHORT_DESC).

    #4154: this used to require an explicit ``if category in (...)`` (or its
    own ``if category == "...":`` branch) per category before that category's
    static verbs became reachable through ``list_actions``/``tools=`` — even
    though every one of those branches did nothing but call
    ``_enumerate_static_category(category)`` unconditionally. That "must add a
    branch" step was forgotten SEVEN times (#2032, #2589/#2621, the three
    management categories the comments below used to name individually,
    #3083, #3465, and finally ``task`` in #4154 itself, discovered because
    describe_task/list_tasks/cancel_task were registered + dispatch-wired via
    ``universal_dispatch`` and listed in ``CATEGORIES``, yet invisible to
    every scheme's ``tools=``): a category can be fully dispatchable via
    ``invoke_action`` while remaining permanently undiscoverable here, and
    the existing #3083 regression test only checked ``CATEGORIES`` MEMBERSHIP
    (a category is *listed*), never enumeration REACHABILITY (a category's
    verbs actually come back non-empty) — so ``task`` passed that gate while
    still being invisible. Closing the class instead of the 7th instance:
    ``knowledge`` is the only category left with a genuine runtime
    availability gate (embedding configured or not — #4932 removed
    ``exec``'s own equivalent gate, see ``is_exec_isolated``); every other
    category — present or future — falls through to the unconditional
    static branch at the bottom with nothing left to remember to wire in.
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

    # knowledge category — search_knowledge (FP-0066 P3c,
    # #3247 firm §3/§6). Visible only when embedding is configured, mirroring
    # the exec branch just below (a runtime-gated, not just router-state-
    # excludable, category) — SHARES ``is_search_available`` with
    # search_actions's own visibility gate rather than re-deriving the
    # embedding-config check a second time (firm §6 "set-sharing").
    # ``rs.embedding_provider``/``embedding_model_class`` are the same two
    # signals ``build_resource_caller_state``/``RouterLoop`` already populate
    # for search_actions (non-None exactly when ``embedding.enabled: true``
    # resolved a provider — see ``Session._build_retrieval_bundle``).
    if category == "knowledge":
        if rs is None:
            return []
        embedding_enabled = bool(
            getattr(rs, "embedding_provider", None) is not None
            and getattr(rs, "embedding_model_class", None)
        )
        if not is_search_available(embedding_enabled=embedding_enabled):
            return []
        return _enumerate_static_category("knowledge")

    # exec category — the `exec` tool (FP-0017; renamed from `sandboxed_exec`
    # #3226 Phase 3). #4932 (owner ruling, 2026-08-19): ALWAYS enumerated,
    # same as every other category below — no runtime availability gate any
    # more (FP-0034 §D14-ext's "hide when noop" reversed; is_exec_isolated's
    # own docstring has the full rationale). RouterCallerState.sandbox_backend
    # still feeds the isolation-disclosure text (None = noop = no isolation).
    if category == "exec":
        backend = getattr(rs, "sandbox_backend", None) if rs is not None else None
        short_desc = "Execute a command in a sandboxed environment."
        if not is_exec_isolated(sandbox_backend=backend):
            short_desc += _EXEC_NO_ISOLATION_NOTICE
        return [{"action_name": "exec", "short_description": short_desc}]

    # Every other category (file / web / memory_operation / reyn_repo /
    # multi_agent / mcp / pipeline / skill_management / pipeline_management /
    # presentation_management / plugin_management / embedding / hooks / task
    # / any future addition) has no runtime availability gate — it is always
    # a fixed verb set, always visible once past the exclusion check above.
    # Callers only ever reach this function with a name already validated
    # against CATEGORIES (list_actions / catalog_entries both filter through
    # ``_validate_category_filter`` first), so an unrecognized name here would
    # be a caller bug, not reachable from the LLM; ``_enumerate_static_category``
    # degrades to ``[]`` for it either way (#3026: driven by
    # ``action_names_for_category``, empty for an unknown category).
    return _enumerate_static_category(category)


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
    # operation category (= list_agents / describe_agent /
    # delegate_to_agent).
    "agent.peer": "multi_agent",
    # #3026 — the last two resource categories collapsed into their verb
    # counterparts. A model whose catalog snapshot pre-dates the collapse asks
    # for these by name; the redirect lets it self-correct in one turn.
    # FP-0066 P1b: ``rag_corpus`` -> ``rag_operation`` redirect removed —
    # ``rag_operation`` itself is retired (no replacement category to redirect
    # to; a model asking for either name now gets the plain unknown-category
    # error listing the current CATEGORIES).
    "memory_entry": "memory_operation",
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
# When ``search_actions`` is gated out of ``tools=`` (= operator hasn't set
# ``embedding.enabled: true``, or the configured embedding class points at a
# backend whose extras aren't installed), the LLM has no way to discover
# that semantic search exists. ``list_actions`` is the discovery wrapper the
# LLM does see; we attach a ``hint`` field to its response so the LLM can
# surface the install / config path back to the user. This is the
# "self-service onboarding" bridge in FP-0043 §Component C.

_HIDDEN_STATE_HINT: Final[str] = (
    "Semantic action search (`search_actions`) is currently unavailable "
    "in this session. To enable it, add to reyn.yaml: "
    "`embedding:\\n  enabled: true` — uses the configured "
    "`embedding.default_class` (default `standard` = OpenAI embeddings, "
    "requires `OPENAI_API_KEY`), or point `embedding.default_class` at "
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
    enable-hint (= set ``embedding.enabled: true`` in
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
      ``{items: [{action_name, short_description, description, input_schema},
      ...], total: int}`` — EVERY page item is enriched via ``_describe_one``
      (no longer gated to category-narrowed browses), so an unfiltered browse is
      as actionable as a narrowed one. Token-bounded by the page limit (default
      10).

    Sort is alphabetical by action_name (= pagination stability).
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
    items.sort(key=lambda it: it["action_name"])
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
        one = _describe_one(it["action_name"], ctx, _registry)
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
    RouterLoop builds the index on first turn when the operator has set
    ``embedding.enabled: true`` (FP-0066 §7) and binds the
    index + provider + model class into the ``RouterCallerState``.

    Response shape per §D11:
        ``{items: [{action_name, short_description, score}, ...]}``

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

    FP-0066 P2d (#3247 firm §5/§6): before ``idx.query()`` serves, awaits
    ``IndexCoordinator.search_await(source_id)`` (steady-state clean =
    cheap no-op; dirty/building = heals/awaits first) and emits
    ``semantic_search_started``/``semantic_search_complete`` (results
    count) audit-events around the query.
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

    # FP-0066 P2d (#3247 firm §5/§6): await the Coordinator's search-await
    # contract before serving — a cheap manifest-read no-op in the steady
    # state (source already "clean"), or a heal-await if a prior sync-in-op
    # build left the source dirty/mid-building (the "best-effort search is
    # a bug" completeness guarantee). Wrapped in semantic_search_started/
    # _complete audit-events (results count) via the shared
    # ``emit_wrapped_semantic_search`` helper (P3-helper, #3247 firm §6) —
    # the unification of this wrap with the ``RouterLoop.search_actions``
    # copy, which ALSO fixes a bug this call site used to have: no
    # try/finally meant a query failure emitted ``semantic_search_started``
    # without its matching ``_complete``. The helper guarantees
    # ``_complete`` fires (``results=0``) even on failure, then re-raises
    # (this handler's own error handling is unchanged — it still propagates).
    from reyn.data.index.coordinator import emit_wrapped_semantic_search, get_index_coordinator

    source_id = getattr(idx, "source_name", None) or "actions"
    events = ctx.events
    coordinator = (
        get_index_coordinator(ctx.workspace.base_dir) if ctx.workspace is not None else None
    )
    results = await emit_wrapped_semantic_search(
        events=events,
        coordinator=coordinator,
        source_id=source_id,
        index=idx,
        query=query,
        op_ctx=op_ctx,
        model_class=model_class,
        top_k=raw_top_k,
    )

    if category_set:
        from reyn.tools.universal_dispatch import category_of
        filtered: list[dict[str, Any]] = []
        for it in results:
            # #3429: the category is a property of the action, looked up in the
            # membership table — it is no longer a parseable prefix of the name.
            cat = category_of(str(it.get("action_name", "")))
            if cat is None:
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
    action_name: str, ctx: ToolContext, registry: Any,
) -> "dict[str, Any] | None":
    """Resolve ``{description, input_schema}`` for one action.

    The shared selection-grade core of ``describe_action`` AND ``list_actions``'
    enriched items, so the two return the SAME description + schema for a given
    action BY CONSTRUCTION (list ≡ describe). Returns ``None`` when the name is
    not a catalog action or has no registry entry (the caller skips / errors as
    it sees fit). Intentionally returns ONLY description + input_schema — the
    describe_action metadata block and the B41 post-call directive stay in
    ``describe_action`` and are not carried into ``list_actions`` items.

    #3026 + #3429: an action's description + schema is simply its
    ToolDefinition's, because the action IS the tool — one name, one schema, on
    every route. The former per-resource override pair
    (``_resource_description`` / ``_resource_input_schema``) existed to paper
    over resource actions whose target was a generic dispatcher; with the
    resource categories collapsed there is no such action left, so the override
    seam is gone rather than kept as an unused hook. ``ctx`` stays in the
    signature: it is what ``list_actions`` / ``catalog_entries`` already thread,
    and removing it would churn both call sites for nothing — and (#4932) it is
    now genuinely read: ``exec``'s description gets an isolation-disclosure
    suffix derived from ``ctx.router_state.sandbox_backend``, the SAME source
    ``_enumerate_category``'s ``"exec"`` branch reads for its own
    ``short_description`` suffix — one signal, two call sites, list ≡ describe
    still holds because both derive from the same ``ctx``.
    """
    from reyn.tools.universal_dispatch import is_known_action

    if not is_known_action(action_name):
        return None
    target = registry.lookup(action_name)
    if target is None:
        return None

    description = target.description
    if action_name == "exec":
        # #4932: same isolation-disclosure suffix _enumerate_category's exec
        # branch appends to short_description — kept here too so
        # describe_action's full description (this function, not the
        # enumeration layer) also discloses it, not just the browse view.
        rs = ctx.router_state
        backend = getattr(rs, "sandbox_backend", None) if rs is not None else None
        if not is_exec_isolated(sandbox_backend=backend):
            description = description + _EXEC_NO_ISOLATION_NOTICE

    return {
        "description": description,
        # #3383: a LIVE LLM-payload seam, not just a tool-result one —
        # ``catalog_entries`` below reuses this ``input_schema`` as an entry's
        # ``parameters``, and the DEFAULT ``enumerate-all`` scheme concatenates
        # ``catalog_entries()`` into the advertised ``tools_channel`` → ``tools=``. Route
        # through the one projection that owns the deep-copy obligation.
        "input_schema": parameters_for_export(target.parameters),
    }


def catalog_entries(ctx: ToolContext) -> list[dict[str, Any]]:
    """Every usable action as a FLAT generic tool-schema dict
    ``{name, description, parameters}`` — the #1593 ``SchemeOps.catalog_entries``
    projection a scheme presents however it likes (enumerate-all flat, CodeAct
    code-API, retrieval subset). Exposes the **actions**, not the 12-category
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
    AVAILABILITY via ``excluded_categories`` (#4932: ``exec``'s sandbox
    backend no longer gates availability, only its description text — see
    ``is_exec_isolated``) — so a None ``router_state`` yields a
    superset-shaped list that is not the "usable this session" set. Note the
    count does NOT depend on how much the operator has accumulated; that
    invariant is pinned in
    ``tests/tools/test_resource_collapse_invariant_3026.py``.
    """
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    entries: list[dict[str, Any]] = []
    for category in CATEGORIES:
        for item in _enumerate_category(category, ctx):
            action_name = item["action_name"]
            one = _describe_one(action_name, ctx, registry)
            if one is None:
                # Unresolvable action (no registry entry) — not a usable entry.
                continue
            parameters = one.get("input_schema")
            if not isinstance(parameters, dict):
                # Completeness bar: a valid no-arg signature, never None.
                parameters = {"type": "object", "properties": {}}
            entries.append({
                "name": action_name,
                "description": one.get("description") or "",
                "parameters": parameters,
            })
    entries.sort(key=lambda entry: entry["name"])
    return entries


async def _handle_describe_action(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """describe_action handler — return the action's description + input_schema.

    Per FP-0034 §D11, returns ``{description, input_schema, metadata}``, read
    straight off the named action's ToolDefinition.

    #3026 + #3429: that is the whole story — the action IS the tool, under one
    name, so ``.parameters`` is always the right schema. The D2-full
    per-resource override (look the schema up from ``ctx.router_state`` because
    the target was a generic dispatcher) went with the resource categories it
    served; see ``_describe_one``. The response no longer carries a
    ``metadata.target_tool_name``: it existed to name the tool the *qualified*
    spelling resolved to, and there is nothing left for it to differ from.

    For an unknown action_name, returns the §D12 error-with-suggestions
    response.
    """
    action_name = args.get("action_name")
    if not action_name:
        return _missing_action_name_error()

    # Lazy imports for circular-dep safety
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import (
        UnknownActionError,
        require_known_action,
    )

    try:
        require_known_action(action_name)
    except UnknownActionError as exc:
        # Augment suggestions with router_state-aware candidates
        return _build_error_response(_augment_suggestions(exc, ctx))

    registry = get_default_registry()
    target = registry.lookup(action_name)
    if target is None:
        return _build_error_response(UnknownActionError(
            action_name,
            f"action {action_name!r} is a catalog member but is not in the "
            f"registry — the membership table and the registry have drifted",
        ))

    # D2-full / B42-NF-W7-1: description + input_schema come from the shared
    # _describe_one core, so describe_action and list_actions' enriched items
    # agree BY CONSTRUCTION. ``one`` is non-None here — the membership +
    # registry lookup above already succeeded, so _describe_one's same
    # resolution does too.
    one = _describe_one(action_name, ctx, registry) or {}

    return {
        "action_name": action_name,
        "description": one.get("description"),
        "input_schema": one.get("input_schema"),
        "metadata": {
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
    """invoke_action handler — run the named action's own handler.

    Wiring:
      1. Check ``action_name`` is a catalog action (``require_known_action``).
      2. Look up its ToolDefinition in the unified registry.
      3. Invoke ``target.handler(args, ctx)``.

    #3429: there is no name rewriting and no arg transform between (1) and (3).
    There used to be — the action name arrived in a ``<category>__<verb>``
    spelling this layer mapped to a flat registry name, and two of the mappings
    also reshaped args (``cluster``→``path``, ``message``→``request``) in ways
    no advertised schema declared. The spelling is gone; the args the model
    sends are the args the handler receives, which is what "transparent
    wrapper" was always supposed to mean.

    The ToolContext is forwarded verbatim so router_state callbacks
    (= send_to_agent / op_context_factory / list_memory_fn / etc.)
    reach the handler as if the caller had invoked it directly.

    Unknown action_name → §D12 error-with-suggestions response.
    """
    action_name = args.get("action_name")
    if not action_name:
        return _missing_action_name_error()

    inner_args = args.get("args") or {}

    # Lazy imports for circular-dep safety
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import (
        UnknownActionError,
        require_known_action,
    )

    try:
        require_known_action(action_name)
    except UnknownActionError as exc:
        return _build_error_response(_augment_suggestions(exc, ctx))

    registry = get_default_registry()
    target = registry.lookup(action_name)
    if target is None:
        return _build_error_response(UnknownActionError(
            action_name,
            f"action {action_name!r} is a catalog member but is not in the "
            f"registry — the membership table and the registry have drifted",
        ))

    # Forward ctx verbatim — target handlers consume their slice of
    # router_state via the typed sub-object.
    result = await target.handler(dict(inner_args), ctx)
    # FP-0056 PR-F1: tag the INVOKED action so canonicalization dispatches by the true
    # invoked identity, not the ``invoke_action`` wrapper (which would resolve to the wrapper's own
    # passthrough declaration and hide the target's text body). The chat/pipeline chokepoints strip
    # this before rendering; dispatch()'s outer tag defers to it (setdefault).
    if isinstance(result, dict) and "_canonical_source" not in result:
        result = {**result, "_canonical_source": action_name}
    return result


def _augment_suggestions(
    exc: "UnknownActionError", ctx: ToolContext,
) -> "UnknownActionError":
    """Re-suggest using router_state-aware candidates when available.

    The PR-2 default suggestion pool is the static catalogue
    (= KNOWN_ACTION_NAMES). This re-derives it from the live
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
            candidates.append(item["action_name"])

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
    gates=ToolGates(router="allow"),
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
    gates=ToolGates(router="allow"),
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
    gates=ToolGates(router="allow"),
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
    gates=ToolGates(router="allow"),
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
    "is_search_available",
    "is_exec_isolated",
    "visible_categories",
]
