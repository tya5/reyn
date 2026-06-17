"""Qualified-name dispatch routing — FP-0034 Phase 1 PR-2.

This module is the **pure-function routing layer** that maps a
qualified action name (= ``<category>__<entry_name>``) to the existing
ToolDefinition handler that fulfils it, plus any arg transformation
the category's canonical semantic prescribes (= D19 resource invoke).

PR-2 scope (this file):
  - ``ResolvedAction`` dataclass — target tool name + transformed args
  - ``resolve_invoke_action`` — routes any qualified name to its target
  - ``resolve_describe_action`` — routes describe target (= same lookup
    table, returns the canonical target tool name for the introspection
    surface)
  - ``ACTION_ROUTING`` table — declarative mapping of category +
    entry-pattern → target tool name + arg transform per FP-0034
    §"Qualified name format" + §D19 canonical default semantic
  - ``suggest_similar_names`` — D12 error-with-suggestions helper using
    difflib similarity ranking
  - ``UnknownActionError`` — raised when routing fails, carries
    suggestions for the caller to surface

This layer is **pure**: no I/O, no state, no live invocation. It is
the design fault-line between the static schema (= PR-1) and the live
runtime wire-up (= PR-3 router_loop integration). Tests verify
routing decisions for all 13 categories without invoking any handler.

Not in PR-2:
  - Wire-up to ``ctx.router_state`` callables (= PR-3)
  - Dynamic enumeration of skills/agents/mcp-tools/memory-entries/
    rag-corpora (= caller-state dependent, PR-3)
  - The ``list_actions`` enumeration body (= PR-3 with caller-state
    integration)
  - ``mcp.operation__drop_server`` op handler (= PR-4 new op)
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
    (= e.g. ``"invoke_skill"``, ``"call_mcp_tool"``, ``"read_file"``).
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


def _invoke_skill_args(entry_name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """``skill__<name>`` → ``invoke_skill({name, input})``.

    The user-supplied ``args`` dict either:
      (a) carries a ``input`` field — used verbatim;
      (b) is empty / lacks ``input`` — treated as the input artifact's
          ``data`` payload (= caller convenience: ``invoke_action(
          "skill__foo", {"x": 1})`` reads as "run skill foo with input
          data {x: 1}").

    Per the existing invoke_skill schema, the target needs
    ``{name, input}`` where ``input`` is ``{type, data}`` artifact dict.
    PR-2 keeps the shape simple: if ``input`` is provided, pass through;
    otherwise wrap ``args`` as the input payload directly (= the artifact
    builder lives caller-side; PR-3 / runtime wire-up will choose the
    final convention).
    """
    if "input" in args:
        return {"name": entry_name, "input": args["input"]}
    return {"name": entry_name, "input": dict(args)}


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


def _read_memory_body_args(
    entry_name: str, args: Mapping[str, Any],
) -> dict[str, Any]:
    """``memory_entry__<name>`` → ``read_memory_body({layer, slug})``.

    D19 resource invoke: invoking a memory entry returns its body.

    The qualified-name format ``memory_entry__<slug>`` does not encode a
    layer; we default to "shared" because that is what the
    ``memory_operation__remember_shared`` write surface produces, and
    therefore what users encounter from natural-language "remember X"
    requests (= e2e-coder 2026-05-17 N4 probe). Agent-layer entries
    require a separate alias namespace (= follow-up if a real probe
    surfaces the gap).
    """
    return {"layer": "shared", "slug": entry_name}


def _recall_single_source_args(
    entry_name: str, args: Mapping[str, Any],
) -> dict[str, Any]:
    """``rag_corpus__<name>`` → ``recall({sources: [name], query, top_k?})``.

    D19 resource invoke: invoking a rag corpus performs a single-source
    recall against that source. The caller passes ``query`` and
    optionally ``top_k``; the source is curried from the entry name.
    """
    out: dict[str, Any] = {"sources": [entry_name]}
    if "query" in args:
        out["query"] = args["query"]
    if "top_k" in args:
        out["top_k"] = args["top_k"]
    return out


def _mcp_tool_args(entry_name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """``mcp__<server>__<tool>`` → ``mcp_call_tool({tool, tool_args})`` (#1647).

    First-class per-tool MCP action. The LLM passes the target MCP tool's OWN
    parameters as the top-level ``args`` (matching the tool's real inputSchema,
    surfaced by describe_action) — ONE args level, no generic ``server`` /
    ``mcp_tool_name`` / nested-args foot-gun (#1646). This shaper wraps them into
    the EXISTING ``mcp_call_tool`` verb's shape: the ``<server>__<tool>``
    identifier (= entry_name, already in that form) under ``tool``, the tool's
    params under ``tool_args``. Dispatch then routes through the SAME
    ``mcp_call_tool`` ToolDefinition that ``mcp__call_tool`` uses → identical
    permission gate + MCPClient path (zero new dispatch/gate surface — the
    per-tool action is purely a catalog/args ergonomics layer over call_mcp_tool).
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
# Maps qualified-name **prefix** to a routing rule. Two flavours:
#
#   (a) Resource categories (= D19): the routing rule is one rule per
#       *category* and the rule uses the entry_name as the resource id.
#       Example: ``mcp.server`` → list_mcp_tools, entry_name=server name.
#
#   (b) Operation categories: the routing rule is per **qualified name**
#       (= category + entry_name), each mapping to its own target tool.
#       Example: ``file__read`` → read_file, ``web__search`` → web_search.
#
# Each rule is a tuple ``(target_tool_name, arg_transformer)``.
#
# Categories with multiple discrete entry-name choices (file, web,
# memory_operation, reyn_source, rag_operation) list each pair
# explicitly. Resource categories (skill, agent.peer, mcp.server,
# mcp.tool, memory_entry, rag_corpus) use the entry_name as the
# resource id and so have a single rule per category.

# Per-category default rule (= used when entry_name is the resource id)
# Issue #879: mcp.server / mcp.tool resource rules removed; verb actions
# in the new ``mcp`` category live in _OPERATION_RULES below.
# Phase 1 follow-up (2026-05-25): ``agent.peer`` collapsed into the
# ``multi_agent`` verb category; resource rule removed.
_RESOURCE_RULES: Final[dict[str, tuple[str, Callable[[str, Mapping[str, Any]], dict[str, Any]]]]] = {
    "skill":         ("invoke_skill",        _invoke_skill_args),
    "memory_entry":  ("read_memory_body",    _read_memory_body_args),
    "rag_corpus":    ("recall",              _recall_single_source_args),
    # #1647: per-tool MCP actions ``mcp__<server>__<tool>`` resolve here (the
    # full name is NOT a static verb in _OPERATION_RULES, so it falls through to
    # this per-category rule). entry_name = ``<server>__<tool>`` → wrapped into
    # the existing mcp_call_tool verb (same gate). Static mcp__* verbs
    # (mcp__call_tool / mcp__list_tools / …) match _OPERATION_RULES FIRST, so they
    # are unaffected; only dynamic per-tool names reach this rule.
    "mcp":           ("mcp_call_tool",       _mcp_tool_args),
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

    # reyn_source category — §D20 surface: read / list / glob / grep.
    # FP-0038 closed the glob / grep gap (= S2 + S3).
    "reyn_source__read": ("reyn_src_read", _passthrough_args),
    "reyn_source__list": ("reyn_src_list", _passthrough_args),
    "reyn_source__glob": ("reyn_src_glob", _passthrough_args),
    "reyn_source__grep": ("reyn_src_grep", _passthrough_args),

    # rag_operation category
    "rag_operation__recall":      ("recall",       _passthrough_args),
    "rag_operation__drop_source": ("drop_source",  _passthrough_args),

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

    # validation category — lint op exposed to the router so users can request
    # skill linting directly ("lint the foo skill").  skill_path accepts a
    # skill name (resolved via the standard reyn/local → project → stdlib
    # search path) or a workspace-relative directory path.
    "validation__lint": ("lint", _passthrough_args),

    # exec category (FP-0017 sandboxed_exec, D14 visibility gating).
    "exec__sandboxed_exec": ("sandboxed_exec", _passthrough_args),
}


# ── KNOWN_QUALIFIED_NAMES — static catalogue for suggestion / list ────────
#
# This is the set of qualified names that PR-2 can route statically
# (= without consulting runtime caller state). Used by
# ``suggest_similar_names`` when callers don't supply a candidate list.
# Dynamic items (skill__*, multi_agent__*, mcp__* tools, memory_entry__*,
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
      2. Look up the category in _OPERATION_RULES (= the full
         qualified name has an explicit per-op routing rule) — if found,
         apply that rule.
      3. Else, look up the category in _RESOURCE_RULES (= the category
         has a per-category D19 resource invoke semantic) — if found,
         apply that rule with entry_name as the resource id.
      4. Else, raise UnknownActionError.

    Args:
        qualified_name: ``<category>__<entry_name>`` per §D18.
        args: Caller-supplied arg dict; transformed per the category
            rule. May be None / empty for resources whose canonical
            invoke takes no args (= memory_entry__foo).

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

    # Fall back to per-category rule (= resource categories, D19)
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

    Resource categories return the canonical invoke target (= what
    invoke_action would call) so describe shows the same surface the
    LLM will actually use.
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

    Resource categories (skill / agent.peer / mcp.{server,tool} /
    memory_entry / rag_corpus) return an empty tuple because their
    entries are dynamic (= populated by caller state in PR-3).

    Operation categories (file / web / memory_operation / reyn_source /
    rag_operation / mcp.operation / exec) return the qualified names
    this module has routing rules for. ``mcp.operation`` returns
    ``("mcp.operation__drop_server",)`` (= PR-4 landed). ``exec``
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


__all__ = [
    "ResolvedAction",
    "UnknownActionError",
    "resolve_invoke_action",
    "resolve_describe_action",
    "suggest_similar_names",
    "KNOWN_STATIC_QUALIFIED_NAMES",
    "known_qualified_name_for_category",
]
