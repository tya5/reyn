"""Qualified-name dispatch routing — FP-0034 Phase 1 PR-2.

This module is the **pure-function routing layer** that maps a
qualified action name (= ``<category>__<entry_name>``) to the existing
ToolDefinition handler that fulfils it, plus any arg transformation
the target's arg shape needs.

**#3026 — resolution and enumeration are different things.** This module
RESOLVES a name the caller already has. It does not decide what the LLM is
SHOWN; that is ``universal_catalog._enumerate_category``. The payload
invariant — the number of tools the LLM is sent must not depend on how many
memories / corpora / MCP tools / pipelines the operator has accumulated — is
therefore a property of the ENUMERATOR, and #3026 fixes it there.

``_OPERATION_RULES`` (below) is the enumerated surface: a closed table of full
literal names, and the ONLY table any enumerator may read.
``_RESOURCE_RULES`` survives for author-time names only — ``pipeline__<name>``
is taught in the user guide, and ``tool: mcp__echo__ping`` is a supported
pipeline DSL step — because resolving a name a human typed costs zero tools.
Collapsing the two concepts would break those surfaces for no invariant gain.

**#879 → #1647 is the cautionary tale, and it is an ENUMERATION story.**
#879 collapsed the mcp resource categories. #1647 re-added an enumerated
action per MCP tool, citing (a) ``call_mcp_tool``'s double-``args`` foot-gun,
which #1646 had fixed two days earlier by renaming the inner param to
``tool_args``, and (b) the need to show each tool's real ``inputSchema``,
which #879 had ALREADY solved by shipping ``inputSchema`` verbatim in
``list_mcp_tools``' result explicitly so no ``describe_mcp_tool`` round-trip
is needed (see ``tools/mcp.py``). Its design note says it mirrors
``skill__<name>`` — a category that has never existed. Both motivations were
dead on arrival; only nobody re-checked. Before enumerating per-resource
actions again, verify the motivating gap is still open — twice now it was
not — and note that ``tests/test_resource_collapse_invariant_3026.py`` will
fail if you do.

PR-2 scope (this file):
  - ``ResolvedAction`` dataclass — target tool name + transformed args
  - ``resolve_invoke_action`` — routes any qualified name to its target
  - ``resolve_describe_action`` — routes describe target (= same lookup
    table, returns the canonical target tool name for the introspection
    surface)
  - ``_OPERATION_RULES`` table — declarative mapping of a full qualified
    name → target tool name + arg transform per FP-0034
    §"Qualified name format"
  - ``suggest_similar_names`` — D12 error-with-suggestions helper using
    difflib similarity ranking
  - ``UnknownActionError`` — raised when routing fails, carries
    suggestions for the caller to surface

This layer is **pure**: no I/O, no state, no live invocation. It is
the design fault-line between the static schema (= PR-1) and the live
runtime wire-up (= PR-3 router_loop integration). Tests verify
routing decisions without invoking any handler.

Because the table is closed (#3026), routing is also independent of
``ctx.router_state``: resolving a name no longer consults what the operator
has accumulated. Enumeration still reads caller-state — but only to decide
whether a FIXED verb is available this session, never to mint new names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any, Callable, Final, Mapping

from reyn.tools.universal_catalog import (
    CATEGORIES,
    split_qualified_name,
)

# ── Routing result + errors ────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedAction:
    """Result of routing a qualified action name to a target ToolDefinition.

    ``target_tool_name`` is the canonical name in get_default_registry()
    (= e.g. ``"mcp_call_tool"``, ``"read_file"``, ``"web_search"``).
    ``target_args`` is the dict of arguments to pass to that target's
    handler after any category-specific transformation (= D19 resource
    invoke or qualified-name expansion).
    """
    target_tool_name: str
    target_args: Mapping[str, Any] = field(default_factory=dict)


class UnknownActionError(ValueError):
    """Raised by resolve_* when routing fails.

    Carries the original ``action_name``, the ``reason`` (= why the
    routing could not complete), and a list of ``suggestions`` from
    nearby qualified names (= D12 error-with-suggestions). Callers
    (= PR-3 router invoke_action handler) format this into the
    LLM-facing error response shape that FP-0034 §D12 prescribes.
    """

    def __init__(
        self,
        action_name: str,
        reason: str,
        suggestions: list[str] | None = None,
    ) -> None:
        self.action_name = action_name
        self.reason = reason
        self.suggestions = suggestions or []
        msg = f"Unknown action {action_name!r}: {reason}"
        if self.suggestions:
            msg += f". Suggestions: {self.suggestions}"
        super().__init__(msg)


# ── Arg transformers (per-category canonical semantic) ────────────────────
#
# Each transformer takes (entry_name, args) and returns the arg dict that
# the target ToolDefinition handler expects. The transformers are pure
# data shapers — they do NOT call handlers, do NOT consult runtime state.


def _multi_agent_list_peers_args(
    entry_name: str, args: Mapping[str, Any],
) -> dict[str, Any]:
    """``multi_agent__list_peers`` → ``list_agents({path})``.

    The underlying ``list_agents`` ToolDefinition requires a ``path``
    argument (empty for top-level clusters, cluster name for agents in
    that cluster). For the verb-action surface, the LLM passes an
    optional ``cluster`` arg or nothing at all; default to ``""`` so
    the common "list all peers" path is a zero-arg call.
    """
    return {"path": args.get("cluster", args.get("path", ""))}


def _multi_agent_delegate_args(
    entry_name: str, args: Mapping[str, Any],
) -> dict[str, Any]:
    """``multi_agent__delegate`` → ``delegate_to_agent({to, request, ...})``.

    Accepts ``message`` (= universal-catalog convention) and remaps it
    to the underlying ``delegate_to_agent`` handler's legacy ``request``
    key. The verb's user-facing schema is ``{to, request}``; the alias
    here is for forward compatibility with LLMs that still emit
    ``message`` from the pre-#882 era.
    """
    out: dict[str, Any] = {}
    for k, v in args.items():
        out["request" if k == "message" else k] = v
    return out


def _pipeline_run_args(entry_name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """``pipeline__<name>`` → ``run_pipeline({name, input})``.

    An AUTHOR-TIME name, not an enumerated action (#3026). ``pipeline__<name>``
    is the form the user guide teaches (``docs/guide/for-users/write-a-pipeline
    .md``: ``pipeline__greet({name: "Reyn"})``) and the form a ``tool:`` step in
    a pipeline DSL file may carry, so it must keep RESOLVING. It is no longer
    ENUMERATED — the catalog lists ``pipeline__run`` / ``pipeline__list``, never
    one action per registered pipeline — which is the whole of the payload
    invariant. Both forms reach ``run_pipeline`` with identical effective args.
    """
    out: dict[str, Any] = {"name": entry_name}
    if "input" in args:
        out["input"] = args["input"]
    return out


def _mcp_tool_args(entry_name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """``mcp__<server>__<tool>`` → ``mcp_call_tool({tool, tool_args})``.

    An AUTHOR-TIME name, not an enumerated action (#3026). A ``tool:`` step in a
    pipeline DSL file may name an MCP tool directly (``tool: mcp__echo__ping``);
    ``interfaces/cli/commands/pipe.py`` builds a real ``router_state`` precisely
    so such a step resolves, so this rule must stay for that path. It is no
    longer ENUMERATED into the LLM's ``tools=`` — see the module docstring on
    #879 → #1647.

    Wraps the caller's one-level tool params into the EXISTING ``mcp_call_tool``
    verb's shape, routing through the same ToolDefinition + permission gate
    ``mcp__call_tool`` uses (zero extra dispatch/gate surface).
    """
    return {"tool": entry_name, "tool_args": dict(args)}


def _passthrough_args(
    entry_name: str, args: Mapping[str, Any],
) -> dict[str, Any]:
    """Identity transformer — args pass through unchanged.

    Used for categories whose entry_name maps 1:1 to a target tool
    (= file__read → read_file, web__search → web_search, etc.).
    The entry_name itself selects the target via the routing table,
    so args don't need transformation.
    """
    return dict(args)


# ── ACTION_ROUTING — central declarative routing table ────────────────────
#
# Maps a FULL qualified name (= category + entry_name) to a routing rule
# ``(target_tool_name, arg_transformer)``. Example: ``file__read`` →
# read_file, ``web__search`` → web_search.
#
# #3026: this is the ONE routing table — the per-category ``_RESOURCE_RULES``
# flavour is gone (see the module docstring). Every action is a verb whose
# resource id, when it has one, is an ordinary argument (``mcp__call_tool``
# takes ``tool``; ``memory_operation__read`` takes ``layer`` + ``slug``;
# ``pipeline__run`` takes ``name``). Keep it that way: a rule keyed by
# anything other than a full literal name re-opens operator-scaled growth.

# ── Author-time resource rules (resolution ONLY — never enumerated) ────────
#
# #3026: a per-CATEGORY rule, applied when the full qualified name is not in
# _OPERATION_RULES. It exists for exactly one reason: names a HUMAN OR AGENT
# WRITES BY HAND must keep resolving. ``pipeline__<name>`` is taught in the user
# guide and ``tool: mcp__echo__ping`` is a supported pipeline DSL step.
#
# **This table must never be read by an enumerator.** The payload invariant —
# the number of tools the LLM is sent does not depend on how much the operator
# has accumulated — is a property of ENUMERATION (universal_catalog.
# _enumerate_category), not of resolution: resolving a name the caller already
# typed costs zero tools. #1647's mistake was to enumerate what only needed to
# resolve. ``memory_entry`` / ``rag_corpus`` are absent because nothing authors
# those names by hand; their capability is memory_operation__read / list and
# rag_operation__semantic_search / list_sources.
#
# ``tests/test_resource_collapse_invariant_3026.py`` pins both halves: the
# payload stays constant as resources grow, AND these author-time names still
# resolve.
_RESOURCE_RULES: Final[dict[str, tuple[str, Callable[[str, Mapping[str, Any]], dict[str, Any]]]]] = {
    "mcp":      ("mcp_call_tool", _mcp_tool_args),
    "pipeline": ("run_pipeline",  _pipeline_run_args),
}


# Per-qualified-name rule (= category + specific entry_name → specific tool)
# The key is the FULL qualified name. The value is the same tuple shape.
_OPERATION_RULES: Final[dict[str, tuple[str, Callable[[str, Mapping[str, Any]], dict[str, Any]]]]] = {
    # file category — §D20 surface: read / write / delete / list / grep /
    # glob / edit.  FP-0040 (#178) closed the edit gap with unique-string
    # anchor + replace_all flag (= Claude Code style).
    "file__read":   ("read_file",       _passthrough_args),
    "file__write":  ("write_file",      _passthrough_args),
    "file__delete": ("delete_file",     _passthrough_args),
    "file__list":   ("list_directory",  _passthrough_args),
    "file__grep":   ("grep_files",      _passthrough_args),
    "file__glob":   ("glob_files",      _passthrough_args),
    "file__edit":   ("edit_file",       _passthrough_args),

    # web category
    "web__search":  ("web_search",      _passthrough_args),
    "web__fetch":   ("web_fetch",       _passthrough_args),

    # memory_operation category
    "memory_operation__remember_shared": ("remember_shared", _passthrough_args),
    "memory_operation__remember_agent":  ("remember_agent",  _passthrough_args),
    "memory_operation__forget":          ("forget_memory",   _passthrough_args),
    # #3026: the READ half. The category was write-only (remember/forget); the
    # only read surface was the per-entry ``memory_entry__<slug>`` action, which
    # put one LLM tool in ``tools=`` per stored memory. Both targets were already
    # registered ToolDefinitions with no route — they reached the LLM only as
    # ``build_tools`` direct tools, which the wrapper scheme strips.
    #
    # ``memory_operation__read`` is strictly MORE capable than the action it
    # replaces: ``memory_entry__<slug>`` hard-coded ``layer="shared"`` (see the
    # deleted ``_read_memory_body_args``), so an AGENT-layer memory — everything
    # ``memory_operation__remember_agent`` writes — was unreadable through the
    # catalog. Passing ``layer`` explicitly makes both layers reachable.
    "memory_operation__list":            ("list_memory",      _passthrough_args),
    "memory_operation__read":            ("read_memory_body", _passthrough_args),

    # reyn_repo category — §D20 surface: read / list / glob / grep.
    # FP-0038 closed the glob / grep gap (= S2 + S3).
    "reyn_repo__read": ("reyn_repo_read", _passthrough_args),
    "reyn_repo__list": ("reyn_repo_list", _passthrough_args),
    "reyn_repo__glob": ("reyn_repo_glob", _passthrough_args),
    "reyn_repo__grep": ("reyn_repo_grep", _passthrough_args),

    # rag_operation category (FP-0057 Phase 2a: rag_operation__recall renamed
    # rag_operation__semantic_search — clean-break alongside the recall ->
    # semantic_search tool rename, no compat alias)
    "rag_operation__semantic_search": ("semantic_search", _passthrough_args),
    "rag_operation__drop_source":     ("drop_source",     _passthrough_args),
    # #3026: the discovery verb. ``semantic_search`` takes a REQUIRED ``sources``
    # list of operator-chosen corpus names; before this, the only surface naming
    # them was the per-corpus ``rag_corpus__<name>`` action (one LLM tool per
    # corpus). Same shape as skill_management__list (#2971) / mcp__list_tools.
    "rag_operation__list_sources":    ("list_rag_sources", _passthrough_args),

    # task category (#1953 dynamic-wire): the 12 task.* control-IR ops. Each
    # maps to the same-named ToolDefinition (tools/task_ops.py) whose handler
    # bridges to execute_op via a real-session OpContext (assignee/requester CAS).
    "task__create":                     ("task.create",                     _passthrough_args),
    "task__update_status":              ("task.update_status",              _passthrough_args),
    "task__get":                        ("task.get",                        _passthrough_args),
    "task__list":                       ("task.list",                       _passthrough_args),
    "task__add_dependency":             ("task.add_dependency",             _passthrough_args),
    "task__remove_dependency":          ("task.remove_dependency",          _passthrough_args),
    "task__repoint_dependency":         ("task.repoint_dependency",         _passthrough_args),
    "task__abort":                      ("task.abort",                      _passthrough_args),
    "task__heartbeat":                  ("task.heartbeat",                  _passthrough_args),
    "task__register_unblock_predicate": ("task.register_unblock_predicate", _passthrough_args),
    "task__comment":                    ("task.comment",                    _passthrough_args),
    "task__assign":                     ("task.assign",                     _passthrough_args),

    # Issue #879 — single ``mcp`` category. 2026-05-25 install-surface
    # refactor: split ``mcp__install_server`` into 3 verbs along the
    # **source axis** (registry / public package channel / local script);
    # also renamed ``search_server`` → ``search_registry`` so the pair
    # (search_registry, install_registry) is self-evident at list_actions
    # time. The non-install verbs are unchanged.
    "mcp__search_registry":  ("mcp_search_registry",  _passthrough_args),
    "mcp__install_registry": ("mcp_install_registry", _passthrough_args),
    "mcp__install_package":  ("mcp_install_package",  _passthrough_args),
    "mcp__install_local":    ("mcp_install_local",    _passthrough_args),
    "mcp__list_servers":     ("list_mcp_servers",     _passthrough_args),
    "mcp__list_tools":       ("list_mcp_tools",       _passthrough_args),
    "mcp__call_tool":        ("mcp_call_tool",        _passthrough_args),
    "mcp__drop_server":      ("mcp_drop_server",      _passthrough_args),

    # Phase 1 follow-up (2026-05-25) — single ``multi_agent`` category
    # collapsing the old ``agent.peer`` resource shape into three verb
    # actions. All three reuse the existing handlers from catalog.py /
    # delegate_to_agent.py verbatim; the dispatch transforms below cover
    # the small arg-shape differences (= optional ``cluster`` →
    # required ``path``; legacy ``message`` → ``request`` remap).
    "multi_agent__list_peers":    ("list_agents",       _multi_agent_list_peers_args),
    "multi_agent__describe_peer": ("describe_agent",    _passthrough_args),
    "multi_agent__delegate":      ("delegate_to_agent", _multi_agent_delegate_args),

    # exec category (FP-0017 sandboxed_exec, D14 visibility gating).
    "exec__sandboxed_exec": ("sandboxed_exec", _passthrough_args),

    # skill_management category (#2548 PR-C/PR-D: skill directory install verbs).
    # NOTE: skill__ is the RESOURCE category prefix (per-skill dynamic dispatch, e.g.
    # skill__code_review). Management operations use skill_management__ to avoid
    # colliding with that resource namespace — mirrors mcp__ (mgmt) vs mcp.<s>.<t> (res).
    "skill_management__install_local":  ("skill_install_local",  _passthrough_args),
    "skill_management__install_source": ("skill_install_source", _passthrough_args),
    # #2971: the discovery verb. Without it a skill outside the L1 menu had no
    # surface naming it, so it was unreachable rather than merely unadvertised.
    "skill_management__list":           ("skill_list",           _passthrough_args),

    # pipeline_management category: pipeline directory/DSL install verbs
    # (mirrors skill_management__install_local / __install_source above).
    # NOTE: pipeline__ is the RESOURCE category prefix (per-registered-pipeline
    # dynamic dispatch, e.g. pipeline__hello). Management operations use
    # pipeline_management__ to avoid colliding with that resource namespace.
    "pipeline_management__install_local":  ("pipeline_install_local",  _passthrough_args),
    "pipeline_management__install_source": ("pipeline_install_source", _passthrough_args),

    # presentation_management category (proposal 0060 Phase 1 Layer A / A8):
    # register a named presentation template. Single verb (no source/git-fetch
    # counterpart — a blueprint is inline declarative data, never file-backed).
    "presentation_management__install": ("presentation_install_local", _passthrough_args),

    # pipeline category (IS-1: sync + REGISTERED-only run_pipeline;
    # IS-2: async launch in a crash-recoverable driver-session;
    # IS-4: ad-hoc INLINE launches — agent-GENERATED DSL + a static-analysis
    # gate, sync-attached and async, sharing the registered launch downstream).
    # #3026: the discovery verb. ``pipeline__run`` takes a REQUIRED ``name``
    # chosen by the operator; before this, the only surface naming a registered
    # pipeline was the per-pipeline ``pipeline__<name>`` action (one LLM tool per
    # pipeline). Same shape as skill_management__list (#2971).
    "pipeline__list": ("pipeline_list", _passthrough_args),
    "pipeline__run": ("run_pipeline", _passthrough_args),
    "pipeline__run_async": ("run_pipeline_async", _passthrough_args),
    "pipeline__run_inline": ("run_pipeline_inline", _passthrough_args),
    "pipeline__run_inline_async": ("run_pipeline_inline_async", _passthrough_args),
}


# ── KNOWN_QUALIFIED_NAMES — static catalogue for suggestion / list ────────
#
# This is the set of qualified names that PR-2 can route statically
# (= without consulting runtime caller state). Used by
# ``suggest_similar_names`` when callers don't supply a candidate list.
# Dynamic items (multi_agent__*, per-tool mcp__* entries, memory_entry__*,
# rag_corpus__*) live in caller state and are not enumerated here. PR-3
# combines this static set with the dynamic items from RouterCallerState to
# feed the actual suggestion engine. (The legacy agent.peer / mcp.tool /
# mcp.server dotted sub-categories were collapsed into multi_agent / mcp;
# #1456: no current category carries a dot.)

KNOWN_STATIC_QUALIFIED_NAMES: Final[tuple[str, ...]] = tuple(
    sorted(_OPERATION_RULES.keys())
)


# ── Public resolution API ──────────────────────────────────────────────────


def resolve_invoke_action(
    qualified_name: str,
    args: Mapping[str, Any] | None = None,
) -> ResolvedAction:
    """Route ``invoke_action(name, args)`` to a target ToolDefinition + args.

    Steps:
      1. Parse the qualified name (= category, entry_name).
      2. Look up the FULL qualified name in _OPERATION_RULES — if found,
         apply that rule.
      3. Else, raise UnknownActionError.

    #3026: there is no step 3 per-category fallback any more. The old
    ``_RESOURCE_RULES`` table let an unrecognised ``<category>__<anything>``
    resolve by currying ``entry_name`` as a resource id, which is what made
    the action set — and therefore the enumerate-all ``tools=`` payload —
    grow with the operator's memories / corpora / MCP tools / pipelines.
    Every action is now a fixed verb whose resource id rides in ``args``,
    so the routing table is CLOSED and the payload is a constant by
    construction (not by convention). See the module docstring.

    Args:
        qualified_name: ``<category>__<entry_name>`` per §D18.
        args: Caller-supplied arg dict; transformed per the rule.

    Returns:
        ResolvedAction with the target ToolDefinition name and the
        transformed args dict.

    Raises:
        UnknownActionError: when qualified_name parses but no routing
            rule exists, OR when the qualified name fails to parse
            (split_qualified_name raises ValueError, re-wrapped).
    """
    args = args or {}
    try:
        category, entry_name = split_qualified_name(qualified_name)
    except ValueError as exc:
        raise UnknownActionError(qualified_name, str(exc)) from exc

    # Per-qualified-name rule first (= operation categories)
    rule = _OPERATION_RULES.get(qualified_name)
    if rule is not None:
        target_name, transform = rule
        return ResolvedAction(
            target_tool_name=target_name,
            target_args=transform(entry_name, args),
        )

    # Author-time resource name (pipeline DSL / user-guide form) — resolution
    # only; these are never enumerated into the LLM payload.
    rule = _RESOURCE_RULES.get(category)
    if rule is not None:
        target_name, transform = rule
        return ResolvedAction(
            target_tool_name=target_name,
            target_args=transform(entry_name, args),
        )

    # No rule — produce suggestions from the static catalogue
    raise UnknownActionError(
        qualified_name,
        f"no routing rule for category {category!r}",
        suggestions=suggest_similar_names(qualified_name),
    )


def resolve_describe_action(qualified_name: str) -> ResolvedAction:
    """Route ``describe_action(name)`` to the target whose schema to return.

    Same lookup table as ``resolve_invoke_action`` but the returned
    ResolvedAction's ``target_args`` is empty — the caller (PR-3) uses
    ``target_tool_name`` to fetch the target ToolDefinition's
    description + parameters from the registry.

    #3026: the two resolvers share ONE closed table (_OPERATION_RULES), so
    "describable" and "invokable" are the same set by construction — a name
    can no longer describe via a per-category resource fallback that
    invoke resolves differently.
    """
    try:
        category, _entry_name = split_qualified_name(qualified_name)
    except ValueError as exc:
        raise UnknownActionError(qualified_name, str(exc)) from exc

    rule = _OPERATION_RULES.get(qualified_name)
    if rule is not None:
        return ResolvedAction(target_tool_name=rule[0])

    rule = _RESOURCE_RULES.get(category)
    if rule is not None:
        return ResolvedAction(target_tool_name=rule[0])

    raise UnknownActionError(
        qualified_name,
        f"no routing rule for category {category!r}",
        suggestions=suggest_similar_names(qualified_name),
    )


# ── Suggestion engine (D12) ───────────────────────────────────────────────


def suggest_similar_names(
    unknown_name: str,
    candidates: list[str] | None = None,
    top_k: int = 3,
    cutoff: float = 0.4,
) -> list[str]:
    """Return up to ``top_k`` similar qualified names from ``candidates``.

    Backs the FP-0034 §D12 error-with-suggestions response. When
    ``candidates`` is None, uses ``KNOWN_STATIC_QUALIFIED_NAMES`` (= the
    7 operation categories whose entry names PR-2 knows about
    statically). PR-3 will pass a richer candidate list including
    dynamic items from caller state.

    Uses difflib.get_close_matches for similarity ranking — same
    algorithm Python's stdlib uses for its own "did you mean?"
    suggestions, no external dependency.

    Args:
        unknown_name: the qualified name that failed to resolve.
        candidates: list of valid qualified names to search; defaults
            to the static catalogue.
        top_k: max number of suggestions (default 3, matching Python's
            internal "did you mean" UX).
        cutoff: minimum similarity ratio [0.0, 1.0] (default 0.4 =
            balanced precision/recall for the typical name lengths).

    Returns:
        Up to ``top_k`` suggested names, ranked by descending similarity.
        Empty list when no candidate scores above ``cutoff``.
    """
    if candidates is None:
        candidates = list(KNOWN_STATIC_QUALIFIED_NAMES)
    if not candidates:
        return []
    return get_close_matches(unknown_name, candidates, n=top_k, cutoff=cutoff)


# ── Introspection helpers (for tests / future PR-3 integration) ───────────


def known_qualified_name_for_category(category: str) -> tuple[str, ...]:
    """Return the static qualified names PR-2 knows about for ``category``.

    Resource categories (mcp dynamic per-tool entries / memory_entry /
    rag_corpus) return an empty tuple because their entries are dynamic
    (= populated by caller state in PR-3).

    Operation categories (file / web / memory_operation / reyn_repo /
    rag_operation / mcp / exec) return the qualified names this module
    has routing rules for. ``mcp__drop_server`` is a static verb
    (= PR-4 landed). ``exec``
    returns ``("exec__sandboxed_exec",)`` — the route is now wired
    (FP-0034 Phase 2); D14 visibility gating stays in the catalog
    enumeration layer (``_enumerate_category`` checks sandbox_backend).
    """
    if category not in CATEGORIES:
        raise ValueError(
            f"unknown category {category!r}; expected one of {list(CATEGORIES)}"
        )
    prefix = f"{category}__"
    return tuple(
        name for name in KNOWN_STATIC_QUALIFIED_NAMES if name.startswith(prefix)
    )


def unwrapped_tool_name(qualified_name: str) -> "str | None":
    """The bare/unwrapped tool name a ``qualified_name`` (universal-catalog
    ``category__verb``) resolves to via ``invoke_action`` — the SOURCE OF TRUTH for
    the qualified↔bare alias (e.g. ``memory_operation__remember_shared`` →
    ``remember_shared``, ``mcp__install_registry`` → ``mcp_install_registry``).
    ``None`` when the name has no static operation rule.

    The live capability gate matches the *effective resolved name*, which differs by
    scheme (some paths present the qualified catalog name, invoke_action unwraps to the
    bare name). Capability floors that must deny a tool on EVERY path derive both forms
    from here (complete-by-construction) — see #2111."""
    rule = _OPERATION_RULES.get(qualified_name)
    return rule[0] if rule is not None else None


# Reverse of the qualified→bare _OPERATION_RULES map (bare → {qualified, …}), built once.
# A MULTIMAP, not 1:1: today each bare tool has a single qualified spelling, but building
# the reverse as a set means a future SECOND qualified form for some bare tool can't
# silently drop a spelling (which would re-open the completeness gap this closes — a
# security path, so it must not depend on the 1:1 assumption holding).
def _build_bare_to_qualified() -> "dict[str, frozenset[str]]":
    acc: "dict[str, set[str]]" = {}
    for qualified, (bare, _h) in _OPERATION_RULES.items():
        acc.setdefault(bare, set()).add(qualified)
    return {bare: frozenset(quals) for bare, quals in acc.items()}


_BARE_TO_QUALIFIED: "Final[dict[str, frozenset[str]]]" = _build_bare_to_qualified()


def all_invocable_forms(name: str) -> "frozenset[str]":
    """Every invocable form of a tool ``name`` — the bare AND the qualified
    (universal-catalog ``category__verb``) spelling — derived from the ``invoke_action``
    ``_OPERATION_RULES`` source of truth.

    The live capability gate matches the EFFECTIVE resolved name, which differs by scheme
    (some paths present the qualified catalog name; invoke_action unwraps to the bare
    name). A deny/allow specified in EITHER form must therefore cover BOTH, or a dual-form
    tool (``file__*`` / ``mcp__*``) is reachable via the unlisted spelling (#2132 — the
    per-session-narrowing analogue of the #2111 floor's qualified→bare derivation, but
    BIDIRECTIONAL because a spawner's narrowing may be written in either form). A name with
    no static rule (a single-form tool) → just itself."""
    forms = {name}
    rule = _OPERATION_RULES.get(name)
    if rule is not None:
        forms.add(rule[0])  # name is qualified → add its bare alias
    qualified_forms = _BARE_TO_QUALIFIED.get(name)
    if qualified_forms is not None:
        forms |= qualified_forms  # name is bare → add ALL its qualified aliases (multimap)
    return frozenset(forms)


__all__ = [
    "ResolvedAction",
    "UnknownActionError",
    "resolve_invoke_action",
    "resolve_describe_action",
    "suggest_similar_names",
    "unwrapped_tool_name",
    "all_invocable_forms",
    "KNOWN_STATIC_QUALIFIED_NAMES",
    "known_qualified_name_for_category",
]
