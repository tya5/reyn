"""LLM-reachability gate — SSoT for which ``router="allow"`` ToolDefinitions
the LLM can actually reach in the default production configuration, and a
closed registry of the ones that cannot be reached today, each carrying a
falsifiable reason (#3464).

## The defect class (5 prior individually-closed instances)

Being registered in the default ``ToolRegistry`` with ``gates.router="allow"``
does not imply the LLM can ever call the tool — #2032, #3083, #2913, #2875,
#3215 each independently patched one instance of "registered + dispatchable
but never appears on the LLM-visible surface." #3464 is the 6th (cron: 5
tools). This module is the gate that replaces patching instance N+1 forever:
it derives reachability structurally and requires every unreachable tool to
be declared, with a reason, rather than silently open.

## Definition of "LLM-reachable" (measured on current ``main``, not assumed)

A ``router="allow"`` ToolDefinition's ``name`` is reachable in the default
production configuration iff EITHER of two independently-sufficient routes
can deliver it to the model:

  (a) **Direct advertisement** — ``build_tools()`` can place the name
      directly in the ``tools=`` payload. Derived structurally: an AST
      census of every ``<registry>.lookup(...)`` call inside
      ``router_tools.build_tools``, resolving both string-literal
      arguments and names that trace back to a string-literal
      tuple/list assignment (covers the wrapper-name loop, which looks
      up a loop variable rather than a literal). Structural, not one
      execution with one args tuple: several of these tools (the D5-D11
      MCP resource/prompt verbs, ``compact``, ``spawn_session``,
      ``search_actions``) are only emitted under a *particular*
      configuration (MCP servers configured / context near budget /
      embedding enabled) — that is correct conditional gating, not an
      unreachability defect, and executing ``build_tools()`` with one
      fixed set of kwargs cannot tell the two apart (confirmed: doing so
      flags those as "unreachable" too, a false positive this census
      avoids). #5291: ``build_tools``'s own leading positional arg
      (``available_agents``, dead — 0 real consumers) was removed; this
      module's own census never depended on that argument's value, only
      on what parameters MAKE a tool appear, so the removal changes
      nothing here but the call shape in this prose.
  (b) **Dispatch via ``invoke_action``** — the name is a catalog action,
      i.e. a member of ``universal_dispatch.KNOWN_ACTION_NAMES`` (the
      ``_CATEGORY_ACTIONS`` membership table). #3429 abolished the
      qualified spelling this route used to be defined through — the old
      reading was "the bare dispatch target of some ``_OPERATION_RULES``
      qualified name"; with one name per action, ``invoke_action``
      dispatches a catalog member under its own name, so membership IS
      the route. (Measured at the switch-over: the two readings produce
      the same unreachable set — the eight tools below — so this is the
      same route restated, not a widened or narrowed one.)
      ``invoke_action`` is itself advertised via route (a) whenever the
      universal wrappers are enabled, which is the shipped default
      (``ToolUseConfig.universal_wrappers_enabled: bool = True`` in
      ``reyn.config.execution`` — #4552 PR-3: moved from
      ``ActionRetrievalConfig`` in ``reyn.config.embedding``) — so
      dispatching through it is a real, always-available route in the
      default configuration, not a hypothetical one.

**"Any one route suffices" is the deliberate reading** — the issue does
not require a SPECIFIC path, only that the model can reach the tool
SOMEHOW. The MCP ``tool_search_tool`` meta-tool is not an independent
THIRD route: it is a dynamically-selected SUBSET of route (a)'s pool
(it wraps the same MCP-server-derived dicts route (a) already assembles)
— it cannot make an otherwise structurally-unreachable tool reachable, so
it is not modeled as a separate route here. (The hot-list mechanism this
paragraph used to also name here as a subset was discarded — #4552 — a
retired opt-in feature, not a reachability route this gate ever needed
to model on its own terms.)

``gates.router="deny"`` tools (e.g. ``ask_user`` — CLI/internal-only by
design) are out of scope by construction: the census only looks at
``gates.router == "allow"``.

## Measured result on current ``main``

5 tools — the cron family only:

  cron_register, cron_unregister, cron_list, cron_enable, cron_disable

#3464 originally measured 8 (cron 5 + ``embed`` / ``emit_hook_event`` /
``hooks_add``): those 3 were a different-in-KIND, same-class-as-#3083
wiring bug (each tool's own module docstring already asserted LLM-facing
intent; they were simply never added to
``universal_dispatch._CATEGORY_ACTIONS``), filed as #3465 and deliberately
NOT fixed in #3464's PR to avoid touching ``universal_dispatch.py`` /
``router_tools.py`` while PR #3463 (a 444-file alias-removal arc) was open
against those same files. #3465 closed all 3 by adding an ``"embedding"``
and a ``"hooks"`` entry to ``_CATEGORY_ACTIONS`` (route (b)) + a matching
``CATEGORIES`` / ``_enumerate_category`` entry in ``universal_catalog.py``
(so they are enumerable, not just dispatchable) + ``router_dispatched=True``
on the two tools that did not already have it. Only cron's fate remains an
open (A)/(B)/(C) product decision (#3464).

## Exclusive-wrapper mode (#3896) — a SECOND, mode-scoped reachability set

Route (a)'s AST census above answers "reachable under SOME valid parameter
combination" — deliberately existential, per the module docstring above. It
does NOT model ``router_tools.build_tools``'s own §J strip step
(``_wrapper_superseded_tool_names()``, run only when
``universal_wrappers_enabled=True``), so it cannot answer a DIFFERENT, also
real question: "reachable when an operator actually selects exclusive-wrapper
mode" (a category-based exposure scheme with ``universal_wrappers_enabled:
true`` — landed, selectable today, #3429). #3896 found this gap: under that
mode, ``spawn_session`` is stripped from direct advertisement (§J) and has NO
catalog route (never added to ``universal_dispatch._CATEGORY_ACTIONS``) — a
genuine capability loss the plain existential census cannot see, because
existentially ``spawn_session`` IS reachable (under the *other* valid combo,
wrappers off).

:func:`compute_reachable_tool_names_under_exclusive_wrapper_mode` and
:func:`compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode`
answer the mode-scoped question directly, by subtracting the REAL strip set
(imported from ``router_tools``, never re-derived) from route (a) before
union-ing with route (b). Their own closed registry,
``UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS``, is SEPARATE from
``UNREACHABLE_TOOL_REASONS`` above — a tool can be declared in one, the
other, both, or neither; they answer different questions and neither
subsumes the other. Measured result under this mode: the 5 cron tools (still
unreachable regardless of mode) plus ``call_mcp_tool`` / ``describe_mcp_tool``
(their own §J entries' reasons already name their replacement — this is the
INTENDED end state, not a gap).

**#3896's own finding (fixed).** ``spawn_session`` used to be in this set too
— stripped by §J with no compensating catalog route, unlike the two MCP
tools above. Owner ruling (2026-08-13): give it a real catalog route rather
than stop stripping it or accept the loss (option 1 of the (A)/(B)/(C) choice
this module's own registry used to track as open). ``universal_dispatch.
_CATEGORY_ACTIONS["multi_agent"]`` now includes it, so route (b) covers it
even while §J still strips route (a) — it is reachable under this mode
again, and no longer appears in the registry below.
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from typing import Final, Mapping


@dataclass(frozen=True)
class UnreachableToolReason:
    """One closed-vocabulary classification + a falsifiable one-line reason.

    A classification without a reason (or vice versa) is not accepted —
    ``test_llm_reachability_gate_3464.py`` requires both non-empty for every
    declared entry, so a hole cannot be registered as a bare shrug.
    """

    classification: str
    reason: str


# Closed vocabulary. A PR adding an entry to UNREACHABLE_TOOL_REASONS must
# pick one of these — not invent ad hoc prose — so the "why" stays
# machine-checkable as a category, not just human-readable as text.
UNREACHABLE_CLASSIFICATIONS: Final[frozenset[str]] = frozenset({
    # Registered, but the LLM-facing surface is intentionally withheld
    # pending an owner-level product decision about GRANTING the
    # capability — advertising it would change what the LLM can do, not
    # merely fix a bug. Carries the specific (A)/(B)/(C)-style decision
    # the entry is waiting on.
    "PENDING_CAPABILITY_DECISION",
    # Registered + fully described as LLM-facing (the tool's own module
    # docstring already states the intent), but never wired into either
    # reachability route — a same-class instance of the #3464 defect,
    # deliberately deferred rather than fixed in the PR that discovered it
    # (wiring touches the same files a large in-flight rename arc owns).
    # Must carry a tracking issue link; this classification is a queue
    # entry, not a resting state.
    "DEFERRED_WIRING_BUG",
    # Unreachable under its OWN name, but by DESIGN, not by gap: a
    # differently-named tool already covers the identical capability (named
    # in the reason). This is the intended permanent end state, not a queue
    # entry — nothing is pending. Distinguishes "renamed/consolidated on
    # purpose" from PENDING_CAPABILITY_DECISION's "capability itself is
    # withheld."
    "SUPERSEDED_BY_CATALOG_REPLACEMENT",
})


_CRON_REASON = (
    "#3464: 5 cron ToolDefinitions (cron_register/_unregister/_list/_enable/"
    "_disable) are registered router=allow but reach neither build_tools()'s "
    "direct-advertisement census nor the catalog membership table "
    "(universal_dispatch.KNOWN_ACTION_NAMES) that invoke_action dispatches from. "
    "Whether to (A) add a `cron` catalog category (capability grant -- the "
    "LLM could directly operate cron, an owner decision), (B) keep them "
    "intentionally CLI/internal-only, or (C) delete the dead surface is an "
    "open product decision tracked on #3464 itself. This entry records (B) "
    "as the interim state -- withheld on purpose, not forgotten -- pending "
    "that decision."
)

UNREACHABLE_TOOL_REASONS: Final[Mapping[str, UnreachableToolReason]] = {
    "cron_register": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "cron_unregister": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "cron_list": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "cron_enable": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "cron_disable": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
}


_CALL_MCP_TOOL_EXCLUSIVE_WRAPPER_REASON = (
    "router_tools.py's own _WRAPPER_SUPERSEDED_BASE_TOOLS entry for "
    "`call_mcp_tool` names its replacement: `mcp_call_tool` is the catalog's "
    "own definition of the identical call, reachable via invoke_action "
    "(a universal_dispatch.KNOWN_ACTION_NAMES member) whenever exclusive-wrapper "
    "mode is on -- the same mode that strips `call_mcp_tool`'s own direct "
    "route. Unreachable under its OWN name by design, not by gap: the LLM's "
    "capability is unchanged, only the tool name it uses to reach it."
)

_DESCRIBE_MCP_TOOL_EXCLUSIVE_WRAPPER_REASON = (
    "router_tools.py's own _WRAPPER_SUPERSEDED_BASE_TOOLS entry for "
    "`describe_mcp_tool` names its replacement: #879 shipped each tool's real "
    "inputSchema verbatim in `list_mcp_tools`'s own result precisely so no "
    "separate describe round-trip is needed -- `list_mcp_tools` is itself a "
    "universal_dispatch.KNOWN_ACTION_NAMES member, reachable via invoke_action "
    "in exclusive-wrapper mode. Unreachable under its OWN name by design, not "
    "by gap: the capability (a tool's input schema) is still obtainable, "
    "folded into a different tool's response shape rather than a dedicated verb."
)

# #3896: a SECOND, mode-scoped closed registry -- see the module docstring's
# "Exclusive-wrapper mode" section for why this is separate from
# UNREACHABLE_TOOL_REASONS above (different question, not a superset/subset).
UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS: Final[Mapping[str, UnreachableToolReason]] = {
    "cron_register": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "cron_unregister": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "cron_list": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "cron_enable": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "cron_disable": UnreachableToolReason("PENDING_CAPABILITY_DECISION", _CRON_REASON),
    "call_mcp_tool": UnreachableToolReason(
        "SUPERSEDED_BY_CATALOG_REPLACEMENT", _CALL_MCP_TOOL_EXCLUSIVE_WRAPPER_REASON
    ),
    "describe_mcp_tool": UnreachableToolReason(
        "SUPERSEDED_BY_CATALOG_REPLACEMENT", _DESCRIBE_MCP_TOOL_EXCLUSIVE_WRAPPER_REASON
    ),
    # #3896: `spawn_session` used to be declared here (PENDING_CAPABILITY_DECISION)
    # — removed once it gained a real `multi_agent` catalog route (see the
    # module docstring's "Exclusive-wrapper mode" section). It is reachable
    # under this mode again via route (b), so it no longer belongs in this
    # registry at all.
}


def _string_constants(node: ast.AST) -> set[str]:
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def compute_direct_advertisable_tool_names(*, source_text: str | None = None) -> frozenset[str]:
    """Derive route (a): every tool name ``build_tools()`` can place directly
    in the ``tools=`` payload under SOME valid combination of its parameters.

    Structural (AST) census, not one execution with one fixed set of kwargs —
    see the module docstring for why a single ``build_tools()`` call produces
    false positives for correctly-conditional tools (MCP resource verbs,
    ``compact``, ``spawn_session``, ``search_actions``).

    ``source_text`` defaults to the real ``build_tools`` source (via
    ``inspect.getsource``); tests pass a modified string to strip-falsify
    without ever touching the file on disk.
    """
    if source_text is None:
        from reyn.runtime.router_tools import build_tools

        source_text = inspect.getsource(build_tools)

    tree = ast.parse(source_text)

    # Pass 1: every ``NAME = <string-literal-only tuple/list/ifexp>``
    # assignment becomes a lookup-key -> {candidate strings} pool entry.
    # This is what resolves the wrapper-name loop below (``_wrapper_names``
    # is assigned an ``A if cond else B`` tuple-of-literals, not a single
    # literal).
    string_var_pool: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        target: ast.AST | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target, value = node.target, node.value
        if target is None or value is None:
            continue
        strs = _string_constants(value)
        if strs:
            string_var_pool.setdefault(target.id, set()).update(strs)

    # Pass 2: propagate through ``for X in Y:`` when Y is a known pool name,
    # so a loop variable (``_wrapper_name``) inherits its iterable's strings
    # (``_wrapper_names``).
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and isinstance(node.iter, ast.Name):
            if node.iter.id in string_var_pool:
                string_var_pool.setdefault(node.target.id, set()).update(string_var_pool[node.iter.id])

    # Pass 3: every ``<obj>.lookup(<arg>)`` call site — resolve a literal
    # string directly, or a Name via the pool built above.
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "lookup":
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in string_var_pool:
                names |= string_var_pool[arg.id]

    return frozenset(names)


def compute_invoke_action_reachable_tool_names(
    *, action_names: frozenset[str] | None = None
) -> frozenset[str]:
    """Derive route (b): every tool name ``invoke_action`` can dispatch —
    the catalog action set (``universal_dispatch.KNOWN_ACTION_NAMES``, built
    from the ``_CATEGORY_ACTIONS`` membership table).

    #3429: this used to read the bare targets out of the qualified→flat
    ``_OPERATION_RULES`` table. There is no second spelling to resolve any
    more — ``invoke_action`` checks membership and dispatches the name as-is
    — so the route is the membership set itself.

    ``action_names`` defaults to the real table; tests pass a filtered copy
    to strip-falsify this route independently of route (a).
    """
    if action_names is None:
        from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES

        action_names = KNOWN_ACTION_NAMES
    return frozenset(action_names)


def compute_llm_reachable_tool_names(
    *,
    source_text: str | None = None,
    action_names: frozenset[str] | None = None,
) -> frozenset[str]:
    """Union of route (a) and route (b) — see module docstring."""
    return compute_direct_advertisable_tool_names(source_text=source_text) | (
        compute_invoke_action_reachable_tool_names(action_names=action_names)
    )


def compute_router_allow_tool_names() -> frozenset[str]:
    """Every ``ToolDefinition`` name in the default registry with
    ``gates.router == "allow"`` — the census population this gate covers."""
    from reyn.tools import get_default_registry

    return frozenset(tool.name for tool in get_default_registry() if tool.gates.router == "allow")


def compute_unreachable_router_allow_tool_names(
    *,
    source_text: str | None = None,
    action_names: frozenset[str] | None = None,
    allow_names: frozenset[str] | None = None,
) -> frozenset[str]:
    """``router="allow"`` tool names that are NOT reachable by either route.

    ``allow_names`` defaults to the real registry census; tests pass an
    override to simulate "a new tool got registered router=allow" without
    mutating the shared default registry.
    """
    if allow_names is None:
        allow_names = compute_router_allow_tool_names()
    reachable = compute_llm_reachable_tool_names(
        source_text=source_text, action_names=action_names
    )
    return allow_names - reachable


def compute_direct_advertisable_tool_names_under_exclusive_wrapper_mode(
    *,
    source_text: str | None = None,
    superseded_names: frozenset[str] | None = None,
) -> frozenset[str]:
    """Route (a) as it actually is once ``universal_wrappers_enabled=True``
    (#3896) — the plain AST census (:func:`compute_direct_advertisable_tool_names`)
    minus ``router_tools.build_tools``'s own §J strip step
    (``_wrapper_superseded_tool_names()``), which the plain census never
    modeled. See the module docstring's "Exclusive-wrapper mode" section.

    ``superseded_names`` defaults to the real strip set (imported from
    ``router_tools``, never re-derived here); tests pass an override to
    strip-falsify this independently of the plain census."""
    if superseded_names is None:
        from reyn.runtime.router_tools import _wrapper_superseded_tool_names

        superseded_names = _wrapper_superseded_tool_names()
    return compute_direct_advertisable_tool_names(source_text=source_text) - superseded_names


def compute_reachable_tool_names_under_exclusive_wrapper_mode(
    *,
    source_text: str | None = None,
    action_names: frozenset[str] | None = None,
    superseded_names: frozenset[str] | None = None,
) -> frozenset[str]:
    """Union of the stripped route (a) and route (b), under exclusive-wrapper
    mode specifically — the mode-scoped analogue of
    :func:`compute_llm_reachable_tool_names`."""
    return compute_direct_advertisable_tool_names_under_exclusive_wrapper_mode(
        source_text=source_text, superseded_names=superseded_names,
    ) | compute_invoke_action_reachable_tool_names(action_names=action_names)


def compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode(
    *,
    source_text: str | None = None,
    action_names: frozenset[str] | None = None,
    allow_names: frozenset[str] | None = None,
    superseded_names: frozenset[str] | None = None,
) -> frozenset[str]:
    """``router="allow"`` tool names NOT reachable by either route once
    exclusive-wrapper mode strips route (a) — the mode-scoped analogue of
    :func:`compute_unreachable_router_allow_tool_names`. See
    ``UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS`` for the declared,
    tracked holes this currently finds."""
    if allow_names is None:
        allow_names = compute_router_allow_tool_names()
    reachable = compute_reachable_tool_names_under_exclusive_wrapper_mode(
        source_text=source_text, action_names=action_names, superseded_names=superseded_names,
    )
    return allow_names - reachable


__all__ = [
    "UnreachableToolReason",
    "UNREACHABLE_CLASSIFICATIONS",
    "UNREACHABLE_TOOL_REASONS",
    "UNREACHABLE_UNDER_EXCLUSIVE_WRAPPER_MODE_REASONS",
    "compute_direct_advertisable_tool_names",
    "compute_direct_advertisable_tool_names_under_exclusive_wrapper_mode",
    "compute_invoke_action_reachable_tool_names",
    "compute_llm_reachable_tool_names",
    "compute_reachable_tool_names_under_exclusive_wrapper_mode",
    "compute_router_allow_tool_names",
    "compute_unreachable_router_allow_tool_names",
    "compute_unreachable_router_allow_tool_names_under_exclusive_wrapper_mode",
]
