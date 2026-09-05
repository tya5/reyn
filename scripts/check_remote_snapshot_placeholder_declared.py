#!/usr/bin/env python3
"""#5093 — a graceful-degrade placeholder in ``project_remote_snapshot``'s
return dict must have a declared axis (or a cited, permanent exemption),
never a hand-typed literal a producer can silently forget to update.

## The bug class this closes

``project_remote_snapshot`` (``reyn/interfaces/repl/read_model.py``) is
the ONE place a remote client's status snapshot gets built. Several of
its dict values are graceful-degrade PLACEHOLDERS — ``None``/``[]``/``0``
for a key the remote transport genuinely cannot report (session-local
state never projected onto the wire). #5009/#5034/#5094/#5185 each found
the SAME shape independently, each time only after an operator actually
hit the blank pane: a placeholder value is, by construction,
indistinguishable from "genuinely empty right now" — the caller cannot
tell "nothing to show" from "this producer cannot show it" unless a
SEPARATE boolean axis (:class:`~reyn.interfaces.repl.read_model.
ChatReadModelCapabilities`) says so. #5093's own architect ruling: stop
finding these one operator-hit at a time — a placeholder with no
declared axis and no cited exemption is a STRUCTURAL defect, catch it in
CI.

## What counts as a "placeholder" here (lead-coder's corrected definition,
## PR #5097 review — a hand-typed literal moved to ``v.get(key, [])`` is
## STILL a placeholder, not a fix, if the key can genuinely be absent from
## an older server's wire payload)

A dict value is a placeholder if its AST is EITHER:
  (a) a bare literal constant — ``None``/``[]``/``{}``/``0``/``0.0``/``""``/``False``, or
  (b) a call of the shape ``<name>.get(<key-literal>, <placeholder-literal>)``
      (the SAME placeholder shapes as (a), as the 2nd positional arg)

Both shapes share the identical caller-visible defect: the caller cannot
distinguish "the read genuinely produced this value" from "there was
nothing to read". A ``.get(key)`` call with NO default (bare optional
read, e.g. ``v.get("attached_name")``) does NOT match — its default is
Python's own ``None`` supplied by the language, not a placeholder someone
chose to paper over an unsupported case; a producer that wires the key at
all gets whatever the wire actually said, `None` included, uniformly.

## The only 3 remedies (no allowlist that grows unbounded)

For each placeholder-shaped key found, exactly one of these must hold, or
the gate is RED:

1. **Genuinely, unconditionally on the wire — cite ``_WIRE_KEYS``.**
   ``read_model._WIRE_KEYS`` (imported here, never re-typed — architect's
   explicit condition, issuecomment-5384873023: a key that later falls off
   the wire protocol automatically re-enters this gate's scope, no second
   edit needed) is the set of ``.get()`` KEY ARGUMENTS with no possible
   "not yet on the wire" state. Matched against the ``.get()`` call's own
   key argument (not necessarily the dict's own output key — #5771 fixed
   the one case that used to differ, ``cost_usd``'s call reading the
   ``"cost_agent"`` wire key under a different name; see this module's
   own "second axis" section below for why THAT shape needed a separate
   check, not a fix to this remedy).
2. **A declared axis exists.** ``ChatReadModelCapabilities`` has a field
   named ``f"{key}_reported"``, OR the key is one of the hand-maintained
   grouped pairings below (a boolean covering more than one dict key —
   cited to the PR/issue that established the grouping, same discipline
   ``REACHABLE_WITHOUT_LLM_TURN`` in ``check_hooks_declared_reachable.py``
   uses for its own non-1:1 mapping).
3. **Cited as a deliberately-cleared non-fabricating literal.** A SMALL,
   hand-maintained, cited set (#5034's own "cleared, not fabricating"
   list) — a placeholder value here does NOT need its own axis because
   #5034 already reasoned through why it never differs meaningfully from
   a genuine local empty state. Adding to this set is a design decision
   with its own citation, not a way to silence the gate.

## Why this is AST, not intent

The gate does not ask "did the author mean to add a graceful degrade" —
it asks a purely syntactic question (does this value match one of the two
placeholder shapes) and then checks 3 syntactic/lookup facts (membership
in ``_WIRE_KEYS``, a matching dataclass field, membership in the cited
non-fabricating set). Zero false positives on the CURRENT dict (verified
by this script's own test suite); a NEW placeholder-shaped key added
without touching one of the 3 remedies goes red immediately, the same
turn it is written.

## Disclosed gap (not a completeness claim)

The 2 placeholder shapes above cover a bare literal and a single ``.get``
call — they do NOT recurse into a ``Tuple``/``List`` of sub-expressions
(``"usage": (0, 0, v.get("agent_tokens", 0))`` and
``"ctx_recent_usage": (0, 0)`` are both this shape today, and neither is
seen by this gate at all, silently). Both are already covered by a
declared axis in the source (``usage_breakdown_reported``/
``cache_usage_reported`` respectively) via a hand-written comment, not
this gate's own enforcement — a FUTURE change to either that drops the
axis check would not be caught here. Widening the AST walk to recurse
into container literals is future work, not attempted in this PR (#5093's
own scope: the placeholder shapes actually observed at #5009/#5034/#5094's
own findings, all of which were shape (a)/(b), not a nested tuple).

## A second axis (#5771): does the OUTPUT KEY itself ride the wire at all

The gap above — the ``usage``/``ctx_recent_usage`` tuples this gate
cannot see the SHAPE of — turned out to be one symptom of a bigger,
independent question :func:`find_unwired_key_violations` asks instead of
trying to widen the AST walk into container literals: never mind what
SHAPE a value is — is the dict KEY it is assigned to even a real
``project_status`` key? Before #5771 stage②, ``"cost_usd": v.get(
"cost_agent", 0.0)`` passed EVERY remedy above (``"cost_agent"``, the
``.get()`` call's own key ARGUMENT, was genuinely on the wire per
``_WIRE_KEYS``) while the OUTPUT key ``"cost_usd"`` was not a real
``project_status`` key at all — an alias, not a placeholder, invisible
to remedies ①②③ because none of them ever look at the output key's own
name against ``project_status`` (stage② later wired ``cost_usd`` for
real, closing this SPECIFIC instance — the general question this axis
asks stays live for every other key).
#5098's own invariant ("one declaration, not two that can drift apart")
is about exactly this — a SEPARATE, narrower promise than "is this
value's placeholder-ness declared" — so :func:`find_unwired_key_
violations` does not consult ①②③ at all; see its own docstring for the
full reasoning and #5771's own issue thread for why this axis is
deliberately reported wide (every unbacked key), not filtered down to
an allowlisted few."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from reyn.interfaces.repl import read_model as read_model_module
from reyn.interfaces.repl.read_model import (
    _WIRE_KEYS,
    REMOTE_CHAT_READ_CAPABILITIES,
    ChatReadModelCapabilities,
    reported_snapshot_keys,
)
from reyn.interfaces.transport.agui import state as agui_state_module

#: #5093 — keys whose placeholder-shaped value is covered by ONE declared
#: axis that does not share its own name (a single boolean gating more than
#: one dict key, or a dict key whose name differs from its axis field).
#: Each entry cites the PR/issue that established the grouping — see
#: ``ChatReadModelCapabilities``'s own docstring for the full reasoning.
_GROUPED_AXIS_KEYS = {
    # #5094/#5097: one flag covers both halves of the agent roster.
    "agent_names": "agent_roster_reported",
    "session_tree": "agent_roster_reported",
    # #5729: per-session turn_active/iv_waiting rides the SAME roster axis
    # as session_tree above — it is the same registry-derived roster data,
    # always projected together, not a separately-gateable capability.
    "all_sessions_status": "agent_roster_reported",
    # #5094/#5097: one flag covers both halves of the model-class catalog.
    "model_classes": "model_catalog_reported",
    "model_active_class": "model_catalog_reported",
    # #5771 stage②: "session_cached_tokens" used to share this entry with
    # "cache_usage_reported" (#5009) -- REMOVED, not updated to the new
    # split field name, because the key is now genuinely, unconditionally
    # on the wire (_WIRE_KEYS) and this remedy is unreachable for it: a
    # dict entry here is only ever consulted by find_violations's remedy②,
    # which remedy① (the _WIRE_KEYS membership check) already short-
    # circuits before reaching. "ctx_recent_usage" itself is a tuple-
    # shaped value this gate's shape detector does not parse (see module
    # docstring's own disclosed gap) so it never reached this mapping
    # either, before or after.
    # #5034: the hook-pane's 2nd dict key rides the same axis as "hooks".
    "hook_items": "hooks_reported",
    # #5009: the compaction-status callable slot is explicitly "gated by
    # ctx_compaction_reported" per project_remote_snapshot's own inline
    # comment at this key -- the axis name differs from the dict key name.
    "ctx_compaction_status_fn": "ctx_compaction_reported",
    # #5605: compaction progress is reported through the same capability axis
    # as its remote placeholder; the explicit flag distinguishes no fold yet.
    "compaction_progress_raw": "compaction_progress_reported",
    # #4996/#5050: this key predates the *_reported convention (named
    # "intervention_head" on the capabilities class, not
    # "pending_intervention_head_reported") — the METHOD-axis field, not a
    # snapshot-key-suffix one; see ChatReadModelCapabilities's own field.
    "pending_intervention_head": "intervention_head",
}

#: #5034 (architect co-vet, its own issue thread) — keys whose placeholder
#: value was explicitly reasoned through and cleared as NOT fabricating: an
#: empty/zero value here never differs meaningfully from a genuinely empty
#: LOCAL state, so no axis is needed to distinguish "unsupported" from
#: "empty". Citing #4194/#4357 for the 2 keys #5034 itself didn't cover.
_CLEARED_NON_FABRICATING_KEYS = {
    "skills",  # #5034: an empty skill list reads identically either way
    "mcp_servers",  # #5034: same reasoning as "skills"
    "unknown_config_key_count",  # #4194: remote has no client-local reyn.yaml to report
    "unknown_config_keys",  # #4357: same as unknown_config_key_count
    "ctx_source",  # #5034: a label, not a fabricatable figure (defensive:
    # its CURRENT value ("remote") isn't placeholder-shaped at all, so this
    # entry only matters if a future edit narrows it to a bare "")
    "turn_usage_fn",  # #3283 ④: a callable slot (not a data figure), always
    # None remotely -- "the right gutter renders '—' rather than a
    # fabricated figure" per this key's own inline comment
}

_PACKAGE_DIR = Path(read_model_module.__file__).resolve()
_AGUI_STATE_PATH = Path(agui_state_module.__file__).resolve()


def _is_placeholder_literal(node: "ast.expr") -> bool:
    """Shape (a): a bare ``None``/``[]``/``{}``/``0``/``0.0``/``""``/``False``."""
    if isinstance(node, ast.Constant):
        return node.value in (None, 0, 0.0, "", False) or node.value is None
    if isinstance(node, (ast.List, ast.Dict)):
        return len(getattr(node, "elts", getattr(node, "keys", []))) == 0
    return False


def _get_call_placeholder_key(node: "ast.expr") -> "str | None":
    """Shape (b): ``<name>.get(<key-literal>, <placeholder-literal>)`` — returns
    the key-literal string if *node* matches, else ``None``."""
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
        return None
    if len(node.args) != 2:
        return None
    key_arg, default_arg = node.args
    if not (isinstance(key_arg, ast.Constant) and isinstance(key_arg.value, str)):
        return None
    if not _is_placeholder_literal(default_arg):
        return None
    return key_arg.value


def _own_return_statements(func_node: "ast.FunctionDef") -> "list[ast.Return]":
    """#5773: every ``ast.Return`` that belongs to *func_node* ITSELF —
    inside its own ``if``/``for``/``with`` bodies, but NOT inside a nested
    ``def``/``async def``/``lambda`` (a return there belongs to that
    NESTED function, never to *func_node*; a bare ``ast.walk`` conflates
    the two, over-collecting). Order is source order (line number)."""
    found: "list[ast.Return]" = []

    def _walk(node: "ast.AST") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Return):
                found.append(child)
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # a nested scope's own return is not func_node's
            _walk(child)

    _walk(func_node)
    return sorted(found, key=lambda n: n.lineno)


def _find_function_return_dict(
    package_dir: Path, func_name: str,
) -> "tuple[ast.Dict | None, str | None]":
    """Parse *package_dir* and return the ``ast.Dict`` node backing *func_name*'s
    return value — shared by both this gate's checks
    (``project_remote_snapshot``'s own placeholder scan, and
    ``project_status``'s unconditional-key-set scan for
    :func:`find_wire_keys_violations`). Handles both ``return {...}``
    directly AND ``out = {...}; return out`` (``project_status``'s own
    shape) — the LAST top-level assignment to the returned name before the
    ``return`` statement, matching how a reader would resolve it by eye.
    Returns ``(None, error_message)`` if the function, its return, or the
    dict literal it resolves to could not be found.

    #5773 (architect BLOCKING finding, agreed by lead-coder): this used to
    take the FIRST ``ast.Return`` found via a bare ``ast.walk`` -- a real,
    silent hazard shared by EVERY check that calls this helper, not just
    #5773's own new one. An early guard clause (e.g. ``if values is None:
    return {}``) added to either producer in the future would make this
    silently resolve to the WRONG (possibly empty) dict, and every caller's
    own population would just as silently shrink to whatever that early
    return happened to be -- the exact "empty population, still green"
    shape CLAUDE.md's own test-review question 4 names, now closed at the
    SOURCE rather than trusted per-caller. Two independent guards: (1)
    collect every top-level ``Return`` belonging to THIS function (not
    walking into a nested ``def``/``lambda``, where a return would belong
    to a DIFFERENT function entirely) and refuse to guess if there is more
    than one; (2) refuse a return that resolves to a dict literal with ZERO
    keys -- neither producer this gate parses has ever had a legitimately
    empty return, so an empty result here is always a mis-resolution, never
    a real shape to accept quietly."""
    source = package_dir.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(package_dir))

    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            func_node = node
            break
    if func_node is None:
        return None, (
            f"check_remote_snapshot_placeholder_declared: could not find "
            f"{func_name} in {package_dir} -- has it been renamed or moved? "
            f"Update this gate's function-name lookup."
        )

    return_nodes = _own_return_statements(func_node)
    if not return_nodes:
        return None, (
            f"check_remote_snapshot_placeholder_declared: {func_name} has no "
            f"return statement -- update this gate."
        )
    if len(return_nodes) > 1:
        return None, (
            f"check_remote_snapshot_placeholder_declared: {func_name} has "
            f"{len(return_nodes)} return statements (lines "
            f"{[n.lineno for n in return_nodes]}) -- this gate assumes exactly "
            f"one and cannot safely guess which is the real one (an early "
            f"guard-clause return would silently shrink this gate's own "
            f"population). Update this gate to resolve the correct one "
            f"explicitly."
        )
    return_node = return_nodes[0]

    if isinstance(return_node.value, ast.Dict):
        if len(return_node.value.keys) == 0:
            return None, (
                f"check_remote_snapshot_placeholder_declared: {func_name}'s "
                f"return resolves to an EMPTY dict literal at line "
                f"{return_node.lineno} -- neither producer this gate parses "
                f"legitimately returns an empty dict; this is almost "
                f"certainly the wrong return statement or a stubbed/mis-"
                f"parsed source. Refusing to silently treat an empty "
                f"population as a clean one."
            )
        return return_node.value, None

    if isinstance(return_node.value, ast.Name):
        returned_name = return_node.value.id
        return_dict = None
        for node in func_node.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Dict)
                and any(
                    isinstance(t, ast.Name) and t.id == returned_name
                    for t in node.targets
                )
            ):
                return_dict = node.value  # last assignment wins (top-to-bottom walk)
        if return_dict is not None:
            if len(return_dict.keys) == 0:
                return None, (
                    f"check_remote_snapshot_placeholder_declared: {func_name}'s "
                    f"``{returned_name}`` resolves to an EMPTY dict literal -- "
                    f"neither producer this gate parses legitimately returns "
                    f"an empty dict; refusing to silently treat an empty "
                    f"population as a clean one."
                )
            return return_dict, None

    return None, (
        f"check_remote_snapshot_placeholder_declared: {func_name}'s return "
        f"value does not resolve to a dict literal (neither `return {{...}}` "
        f"nor `<name> = {{...}}; return <name>`) -- update this gate."
    )


def find_violations(package_dir: "Path | None" = None) -> "list[str]":
    """Return a message per placeholder-shaped dict key in
    ``project_remote_snapshot`` with none of the 3 remedies. Empty = clean."""
    if package_dir is None:
        package_dir = _PACKAGE_DIR
    return_dict, error = _find_function_return_dict(package_dir, "project_remote_snapshot")
    if return_dict is None:
        return [error or "unknown error locating project_remote_snapshot"]

    axis_field_names = {f.name for f in _dataclass_field_names()}
    violations: "list[str]" = []
    for key_node, value_node in zip(return_dict.keys, return_dict.values):
        if key_node is None:  # a ``**spread`` entry -- exempt by definition
            continue
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue  # a non-string-literal key (none exist today) -- not this gate's shape
        dict_key = key_node.value

        wire_key = _get_call_placeholder_key(value_node)
        if wire_key is not None:
            if wire_key in _WIRE_KEYS:
                continue  # remedy ① -- genuinely, unconditionally on the wire
        elif not _is_placeholder_literal(value_node):
            continue  # not a placeholder shape at all -- not this gate's concern

        if dict_key in _CLEARED_NON_FABRICATING_KEYS:
            continue  # remedy ③
        if f"{dict_key}_reported" in axis_field_names:
            continue  # remedy ②, direct suffix match
        if _GROUPED_AXIS_KEYS.get(dict_key) in axis_field_names:
            continue  # remedy ②, grouped/renamed match

        violations.append(
            f'"{dict_key}": a placeholder-shaped value with no declared axis, no '
            f'_WIRE_KEYS membership, and no cited non-fabricating exemption. Fix: '
            f'add a `{dict_key}_reported` field to ChatReadModelCapabilities (or an '
            f'entry to _GROUPED_AXIS_KEYS if it shares an axis with another key), OR '
            f'add "{dict_key}" to _WIRE_KEYS in read_model.py if it is genuinely, '
            f'unconditionally always on the wire, OR cite a reason and add it to '
            f'_CLEARED_NON_FABRICATING_KEYS here.'
        )
    return violations


def _dataclass_field_names():
    import dataclasses
    return dataclasses.fields(ChatReadModelCapabilities)


def find_wire_keys_violations(agui_state_path: "Path | None" = None) -> "list[str]":
    """#5093 (architect blocking finding, issuecomment-5385179961, PR #5206
    A #1): ``_WIRE_KEYS`` was a hand-typed ASSERTION with no producer code
    reading it -- "these keys are genuinely, unconditionally on the wire"
    was never checked against the actual wire emitter. This closes that:
    ``_WIRE_KEYS`` must be a SUBSET of the keys ``agui/state.py``'s
    ``project_status`` unconditionally emits (every key in ITS OWN return
    dict, since every value there is a ``snap.get(key, default)`` call with
    a default -- the key is always present in the output regardless of
    ``snap``'s own contents). A key later removed from ``project_status``
    but left in ``_WIRE_KEYS`` now goes RED here -- the exact gap the
    architect finding named (the PR body's prior claim that a key falling
    off the wire "re-enters the gate's scope with no second edit" was false
    until this function existed to make it true)."""
    if agui_state_path is None:
        agui_state_path = _AGUI_STATE_PATH
    return_dict, error = _find_function_return_dict(agui_state_path, "project_status")
    if return_dict is None:
        return [error or "unknown error locating project_status"]

    unconditional_keys = {
        key_node.value
        for key_node in return_dict.keys
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)
    }
    missing = sorted(_WIRE_KEYS - unconditional_keys)
    return [
        f'"{key}" is in read_model._WIRE_KEYS but project_status no longer '
        f"unconditionally emits it -- remove it from _WIRE_KEYS (it needs a "
        f"declared ChatReadModelCapabilities axis instead, like any other "
        f"key that can be absent from the wire)"
        for key in missing
    ]


def find_unwired_key_violations(
    package_dir: "Path | None" = None, agui_state_path: "Path | None" = None,
) -> "list[str]":
    """#5771 stage ① — the axis #5093's own 2 existing checks do NOT cover:
    a ``project_remote_snapshot`` OUTPUT key that is not itself a key
    ``project_status`` (agui/state.py) emits, regardless of whether its
    VALUE'S OWN placeholder-ness is properly declared.

    #5093's existing :func:`find_violations` asks "is this value's
    graceful-degrade nature honestly declared" (remedies ①②③, all keyed
    off the VALUE's shape or the ``.get()`` call's own key ARGUMENT).
    #5098's own invariant is a DIFFERENT, narrower promise: ``project_
    status`` is "the SOLE declaration of which keys ride the wire" (that
    function's own docstring, verbatim). Those two questions can give
    opposite answers on the SAME key — before #5771 stage②,
    ``"cost_usd": v.get("cost_agent", 0.0)`` passed remedy① (``"cost_
    agent"`` — the ``.get()`` ARGUMENT — was genuinely, unconditionally
    on the wire, per ``_WIRE_KEYS``) even though the OUTPUT key
    ``"cost_usd"`` itself was not a real ``project_status`` key at all —
    the value was quietly aliased from a DIFFERENT wire fact under a
    name that claimed to be its own (stage② later wired it for real,
    closing this specific instance). #5098's invariant is violated the
    moment ANY output key lacks a same-named ``project_status`` entry,
    independent of whether remedies ①②③ would separately excuse its
    placeholder-ness — so this check does not consult them, deliberately:
    a key can be an honestly-declared placeholder (passing #5093's own
    gate) and STILL be exactly the drift #5098 exists to prevent.

    Deliberately WIDE, deliberately un-filtered (#5771 stage ① scope,
    lead-coder dispatch): this returns EVERY output key without a
    same-named ``project_status`` entry, including ones that are
    genuinely, permanently session-local (``cron_jobs``, ``tasks``,
    etc. — these will likely never gain a ``project_status`` twin, and
    that is fine; #5098's invariant was never "every key must be on the
    wire", only "a key that name-claims to be one thing must not
    silently be a different, unwired one"). Stage ① does not attempt to
    tell the two apart with a per-key exemption list — see
    ``test_5771_unwired_key_violations_expose_the_cost_tab_drift.py``
    for the current disclosed list and #5771's own issue thread for the
    triage split (cost tab keys fixed here; every other exposed key
    filed as its own issue, not silenced by an allowlist invented for
    this PR alone)."""
    if package_dir is None:
        package_dir = _PACKAGE_DIR
    if agui_state_path is None:
        agui_state_path = _AGUI_STATE_PATH

    examined, unanalyzable = _examine_remote_output_keys(package_dir)
    if examined is None:
        return unanalyzable

    status_dict, error = _find_function_return_dict(agui_state_path, "project_status")
    if status_dict is None:
        return [error or "unknown error locating project_status"]
    wire_keys = {
        key_node.value
        for key_node in status_dict.keys
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)
    }

    violations: "list[str]" = list(unanalyzable)
    for dict_key, via_spread in examined:
        if dict_key in wire_keys:
            continue
        if via_spread:
            violations.append(
                f'"{dict_key}" (via **reported_snapshot_keys(...)): '
                f'project_remote_snapshot maps this key through the '
                f'ChatReadModelCapabilities spread, but project_status '
                f'(agui/state.py) has no key of this name -- same #5098 '
                f'drift as a literal key, just reached through the '
                f'spread instead of a direct dict entry.'
            )
        else:
            violations.append(
                f'"{dict_key}": project_remote_snapshot maps this key, but '
                f'project_status (agui/state.py) has no key of this name -- '
                f'#5098\'s "one declaration, not two that can drift apart" is '
                f'broken for this key regardless of whether its placeholder-ness '
                f'is otherwise declared. Either add "{dict_key}" to '
                f'project_status\'s own return dict (if it is genuinely '
                f'real, per-connection wire data), or confirm it is intentionally '
                f'session-local and file/track it separately -- this check does '
                f'not accept a same-file exemption.'
            )
    return violations


def _examine_remote_output_keys(
    package_dir: "Path | None" = None,
) -> "tuple[list[tuple[str, bool]] | None, list[str]]":
    """#5771/#5773: every output key ``project_remote_snapshot``'s return
    dict actually has, as ``(key_name, via_spread)`` pairs — independent
    of whether each is a violation. Split out of :func:`find_unwired_key_
    violations` so a caller can ask "how many keys did this walk even
    examine" (lead-coder BLOCKING, PR #5773: a NON-EXPIRING non-vacuity
    witness — the walk finding SOME keys is true today and stays true
    after stage② fixes the 3 cost-tab keys, unlike asserting those 3
    SPECIFIC keys are still violations, which stops being true the moment
    they're fixed — see :func:`count_examined_output_keys`).

    Returns ``(None, [error])`` if ``project_remote_snapshot`` itself
    could not be parsed. The 2nd element is always the list of entries
    this walk could NOT resolve to a key name at all (an unrecognized
    ``**spread``, a non-literal key) — never silently dropped; these are
    always violations in :func:`find_unwired_key_violations`, but they
    are not key NAMES so they cannot appear in the 1st element."""
    if package_dir is None:
        package_dir = _PACKAGE_DIR
    remote_dict, error = _find_function_return_dict(package_dir, "project_remote_snapshot")
    if remote_dict is None:
        return None, [error or "unknown error locating project_remote_snapshot"]

    examined: "list[tuple[str, bool]]" = []
    unanalyzable: "list[str]" = []
    for key_node, value_node in zip(remote_dict.keys, remote_dict.values):
        if key_node is None:  # a ``**spread`` entry
            spread_keys = _resolve_reported_snapshot_keys_spread(value_node)
            if spread_keys is None:
                unanalyzable.append(
                    "an unrecognized `**spread` entry in project_remote_"
                    "snapshot's return dict cannot be statically analyzed by "
                    "this check -- #5773 (architect BLOCKING finding): a "
                    "silently-skipped spread is a silent gap in this gate's "
                    "own census, not an exemption. Either make this gate "
                    "recognize the new spread shape (see "
                    "_resolve_reported_snapshot_keys_spread), or confirm by "
                    "hand that every key it expands to is backed by a "
                    "same-named project_status key."
                )
                continue
            examined.extend((k, True) for k in spread_keys)
            continue
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            unanalyzable.append(
                f"project_remote_snapshot's return dict has a key at line "
                f"{key_node.lineno} that is not a string literal -- this "
                f"check cannot verify a computed key against project_status "
                f"and refuses to silently skip it (#5773: a skip here is a "
                f"census gap, not a pass)."
            )
            continue
        examined.append((key_node.value, False))
    return examined, unanalyzable


def count_examined_output_keys(package_dir: "Path | None" = None) -> int:
    """#5771 (lead-coder BLOCKING, PR #5773): the non-expiring non-vacuity
    witness for the ratchet below. Counting VIOLATIONS (as the original
    version of this PR's own test did, asserting the 3 known cost-tab
    keys are present) has an expiry date built in: stage② fixing those 3
    keys makes them vanish from the violation list, and a test pinned to
    "these 3 specific keys are still violations" would then have to be
    deleted or weakened — losing the non-vacuity guard at exactly the
    moment a silently-broken walk would look identical to "everything got
    fixed". Counting EXAMINED keys instead never expires: this is > 0
    today, stays > 0 after every currently-known violation is fixed
    (project_remote_snapshot will always have SOME output keys), and only
    goes to 0 if the walk itself regresses."""
    examined, _unanalyzable = _examine_remote_output_keys(package_dir)
    return len(examined) if examined is not None else 0


def _resolve_reported_snapshot_keys_spread(node: "ast.expr") -> "list[str] | None":
    """#5773 (architect BLOCKING finding): recognize the ONE ``**spread``
    shape ``project_remote_snapshot`` actually uses —
    ``**reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)`` — and
    resolve it to its REAL field names via a genuine import + call (not a
    second, hand-typed guess at what it expands to), so
    :func:`find_unwired_key_violations` can check each one same as any
    other output key. Every ``ChatReadModelCapabilities`` field name is
    itself absent from ``project_status`` today (that dataclass's fields
    are declared-axis booleans, never wire keys) — this is precisely the
    "stage③'s own family is invisible to this census" gap the architect
    finding named; surfacing it here, once, at the ONE call site that
    produces it, is cheaper and more honest than trying to keep a second
    hand-typed list of field names in sync with the dataclass. Returns
    ``None`` for any OTHER spread shape (unrecognized -- the caller must
    not silently accept it either)."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "reported_snapshot_keys"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "REMOTE_CHAT_READ_CAPABILITIES"
    ):
        return None
    return sorted(reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES))


#: A key's declared DISPOSITION in :data:`_UNWIRED_KEY_VIOLATIONS_BASELINE`
#: — ``LOCAL`` (permanently session-local; no ``project_status`` twin is
#: ever planned) or ``PENDING`` (real, per-connection data that genuinely
#: could/should ride the wire — tracked for triage on #5774, or, for the 3
#: cost-tab keys, committed to #5771's own stage②). #5771 (lead-coder
#: BLOCKING, PR #5773): a bare set of key NAMES let ``cron_jobs`` (never
#: wired, ever) and ``cost_usd`` (a real fact this PR's own issue commits
#: to wiring) sit as the identical shape — architect's own #5772 finding,
#: "one spelling, two facts", recurring here. The disposition is what
#: makes the repair-obligation message in :func:`find_new_unwired_key_
#: violations` mechanically askable per key, not just a generic reminder.
_LOCAL = "LOCAL"
_PENDING = "PENDING"

#: #5771 (lead-coder BLOCKING, PR #5773 head 2c59bbfc0): "①に無い key を②が
#: map *できない* 形にする" means DETECTING drift is not enough — something
#: must actually STOP a NEW one from landing unnoticed. Without this,
#: :func:`find_unwired_key_violations` only ever produces a number someone
#: has to remember to re-check by hand; stage②'s own 8 new wire keys could
#: introduce a 41st drifted key and nothing here would say so — the exact
#: "adding ② before ① is closed becomes a 4th drift" architect warned about
#: (agreed by lead-coder). This is the KNOWN, currently-reported violation
#: set as of THIS baseline's own last update (:func:`find_new_unwired_key_
#: violations` below is the actual ratchet: real-source violations must be
#: a SUBSET of this set's keys, new is red). SHRINKING this dict (dropping
#: a key once it is genuinely fixed) is always safe and never itself
#: flagged — encouraged, not required, so a fix elsewhere never needs to
#: touch this file.
#:
#: 38 entries (was 40 — #5771 stage② wired ``cost_usd``/``usage``/
#: ``session_cached_tokens`` for real and removed them here, then added 1
#: new spread field, ``session_cache_usage_reported``, split off ``cache_
#: usage_reported`` on the SAME PR; a wired key is no longer unwired-key
#: drift at all, so a baseline entry for it would be a standing lie about
#: the gate's own findings). PENDING (2): ``hooks_config_warnings``/
#: ``mcp_probe_states`` — real session data, not yet wired, filed on
#: #5774 for triage (see their own entries below for the citation).
#: LOCAL (36): 14 literal
#: output keys whose OWN inline comment in ``project_remote_snapshot``
#: already says the underlying data is session-local/client-local by
#: construction (``skills``/``mcp_servers``/``turn_usage_fn`` already sit
#: in ``_CLEARED_NON_FABRICATING_KEYS`` above for the same reason;
#: ``unknown_config_key_count``/``unknown_config_keys`` name the CLIENT's
#: own reyn.yaml, structurally absent on a remote connection; ``ctx_
#: source`` is a label, not fabricatable figure; ``tasks`` is explicitly
#: "never on the wire" per its own comment) — plus all 22
#: ``ChatReadModelCapabilities`` field names reached through the
#: ``**reported_snapshot_keys(...)`` spread, LOCAL by DESIGN rather than
#: by accident: a ``*_reported`` flag is a CLIENT-side declaration about
#: what the wire carries, never wire data itself, so it was never
#: expected to have its own ``project_status`` twin (true even for the 5
#: fields already ``True`` for remote — ``intervention_head``/``agent_
#: roster_reported``/``model_catalog_reported``/``attached_name_
#: reported``/``visibility_items_reported``/``mcp_subscriptions_
#: reported`` — since what they report reflects reaches the wire under
#: OTHER, real key names, e.g. ``agent_names``/``session_tree``, never
#: under the flag's own name). ``hooks_config_warnings`` and ``mcp_probe_
#: states`` are marked PENDING, not LOCAL, DESPITE looking like the other
#: session-local literal keys above — their own inline comments in
#: ``project_remote_snapshot`` explicitly say "not wired onto the wire
#: YET" (not "structurally cannot exist"), the opposite framing the LOCAL
#: entries' own comments use — genuinely different from the other 36,
#: filed on #5774 for real triage rather than guessed here.
_UNWIRED_KEY_VIOLATIONS_BASELINE: "dict[str, str]" = {
    # #5771 stage②: "cost_usd"/"usage"/"session_cached_tokens" REMOVED —
    # all 3 are now genuinely, unconditionally on the wire (project_status
    # emits them for real); find_unwired_key_violations no longer flags
    # them at all, so a baseline entry for them would be a standing lie
    # (lead-coder's own instruction on the stage② dispatch: "残すと gate
    # は緑のまま宣言が嘘になります" — leaving it would keep the gate green
    # while the declaration itself became false).
    #
    # PENDING — real session data, explicitly "not wired onto the wire
    # YET" per project_remote_snapshot's own inline comment (unlike the
    # LOCAL entries below, whose own comments say the opposite) -- #5774
    # triage, not guessed here.
    "hooks_config_warnings": _PENDING,
    "mcp_probe_states": _PENDING,
    # LOCAL — genuinely, permanently session-local per each key's own
    # inline comment in project_remote_snapshot.
    "ctx_recent_usage": _LOCAL,
    "ctx_source": _LOCAL,
    "ctx_compaction_status_fn": _LOCAL,
    "compaction_progress_raw": _LOCAL,
    "turn_usage_fn": _LOCAL,
    "cron_jobs": _LOCAL,
    "mcp_servers": _LOCAL,
    "hooks": _LOCAL,
    "skills": _LOCAL,
    "hook_items": _LOCAL,
    "pipelines": _LOCAL,
    "unknown_config_key_count": _LOCAL,
    "unknown_config_keys": _LOCAL,
    "tasks": _LOCAL,
    # LOCAL by DESIGN — every ChatReadModelCapabilities field name (via
    # the **reported_snapshot_keys(...) spread): a client-side capability
    # DECLARATION, never wire data, so none of these was ever expected to
    # have its own project_status twin (see the block comment above).
    "completion_source": _LOCAL,
    "intervention_head": _LOCAL,
    "pending_command_ui": _LOCAL,
    "has_command_ui_region": _LOCAL,
    "conversation_history": _LOCAL,
    "load_older_conversation_history": _LOCAL,
    "cache_usage_reported": _LOCAL,
    "cron_jobs_reported": _LOCAL,
    "usage_breakdown_reported": _LOCAL,
    "ctx_compaction_reported": _LOCAL,
    "hooks_reported": _LOCAL,
    "pipelines_reported": _LOCAL,
    "agent_roster_reported": _LOCAL,
    "model_catalog_reported": _LOCAL,
    "attached_name_reported": _LOCAL,
    "visibility_items_reported": _LOCAL,
    "mcp_subscriptions_reported": _LOCAL,
    "mcp_probe_states_reported": _LOCAL,
    "hooks_config_warnings_reported": _LOCAL,
    "compaction_progress_reported": _LOCAL,
    "tasks_reported": _LOCAL,
    # #5771 stage②: new field (split from cache_usage_reported) — LOCAL
    # by the same DESIGN reasoning as every other spread field above, a
    # client-side declaration, never wire data.
    "session_cache_usage_reported": _LOCAL,
}


def _unwired_key_names(
    package_dir: "Path | None" = None, agui_state_path: "Path | None" = None,
) -> "set[str]":
    """The bare key-name set behind :func:`find_unwired_key_violations`'s
    own messages — the ONE place that parses them back out, so the
    ratchet below and any future consumer never re-derive the "first
    `"..."`-quoted token" convention independently."""
    violations = find_unwired_key_violations(package_dir, agui_state_path)
    names: "set[str]" = set()
    for v in violations:
        if v.startswith('"'):
            names.add(v.split('"')[1])
    return names


def find_new_unwired_key_violations(
    package_dir: "Path | None" = None, agui_state_path: "Path | None" = None,
) -> "list[str]":
    """#5771's own ratchet (lead-coder BLOCKING): a key ``find_unwired_key_
    violations`` reports that is NOT already a key of :data:`_UNWIRED_KEY_
    VIOLATIONS_BASELINE`. Real-source violations that ARE in the baseline
    are known, disclosed debt (tracked on PR #5773's own comment thread,
    not re-flagged here every run); a NEW one is exactly the "stage② adds
    a key, nothing notices" hole the BLOCKING named — this is what
    actually stops it, not just reports it.

    Same "repair obligation, not a silencing knob" shape as ``_DECLARED_
    SITES``'s own ratchet test in ``test_4401_render_for_router_state_
    census.py``: adding a key to the baseline WITHOUT first either wiring
    it onto ``project_status`` for real (PENDING) or confirming it is
    genuinely, permanently session-local (LOCAL) does not close the gap
    #5098/#5771 exist to close — it only silences this one check."""
    found = _unwired_key_names(package_dir, agui_state_path)
    new = found - set(_UNWIRED_KEY_VIOLATIONS_BASELINE)
    return [
        f'"{key}": a NEW project_remote_snapshot key with no same-named '
        f'project_status entry, not already a key of _UNWIRED_KEY_'
        f'VIOLATIONS_BASELINE. Before adding "{key}" there: if it is '
        f'genuinely, permanently session-local, add it with disposition '
        f'LOCAL and cite the reason (mirroring an existing LOCAL entry\'s '
        f'own comment); if it is real, per-connection data that should '
        f'ride the wire, either add the real key to project_status instead '
        f'of baselining this one, or add it with disposition PENDING and '
        f'cite the issue/PR that will wire it. Adding it to the baseline '
        f'without one of those does not close #5098\'s own "one '
        f'declaration, not two that can drift apart" hole -- it only '
        f'silences this check.'
        for key in sorted(new)
    ]


def main() -> int:
    violations = find_violations(_PACKAGE_DIR)
    wire_key_violations = find_wire_keys_violations(_AGUI_STATE_PATH)
    unwired_key_violations = find_unwired_key_violations(_PACKAGE_DIR, _AGUI_STATE_PATH)
    new_unwired_key_violations = find_new_unwired_key_violations(_PACKAGE_DIR, _AGUI_STATE_PATH)
    if violations or wire_key_violations or new_unwired_key_violations:
        if violations:
            print(
                "check_remote_snapshot_placeholder_declared: "
                f"{len(violations)} undeclared placeholder(s) in "
                "project_remote_snapshot's return dict:\n"
            )
            for v in violations:
                print(f"  - {v}")
        if wire_key_violations:
            print(
                "check_remote_snapshot_placeholder_declared: "
                f"{len(wire_key_violations)} _WIRE_KEYS entry(ies) no longer "
                "backed by project_status:\n"
            )
            for v in wire_key_violations:
                print(f"  - {v}")
        if unwired_key_violations:
            print(
                "check_remote_snapshot_placeholder_declared: "
                f"{len(unwired_key_violations)} project_remote_snapshot key(s) "
                "not backed by a same-named project_status key (#5771, "
                f"{len(new_unwired_key_violations)} of them NEW, not yet "
                "baselined):\n"
            )
            for v in unwired_key_violations:
                print(f"  - {v}")
        if new_unwired_key_violations:
            print(
                "check_remote_snapshot_placeholder_declared: "
                f"{len(new_unwired_key_violations)} NEW unwired-key "
                "violation(s), not in _UNWIRED_KEY_VIOLATIONS_BASELINE:\n"
            )
            for v in new_unwired_key_violations:
                print(f"  - {v}")
        return 1
    print("check_remote_snapshot_placeholder_declared: OK, every placeholder-shaped "
          "key is covered by _WIRE_KEYS, a declared axis, or a cited exemption, "
          "_WIRE_KEYS is a verified subset of project_status's unconditional keys, "
          "and every project_remote_snapshot output key is backed by a same-named "
          "project_status key or already-baselined disclosed debt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
