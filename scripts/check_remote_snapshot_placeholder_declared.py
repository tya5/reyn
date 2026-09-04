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
   key argument (not necessarily the dict's own output key — e.g.
   ``cost_usd``'s call reads the ``"cost_agent"`` wire key).
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
own findings, all of which were shape (a)/(b), not a nested tuple)."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from reyn.interfaces.repl import read_model as read_model_module
from reyn.interfaces.repl.read_model import _WIRE_KEYS, ChatReadModelCapabilities
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
    # #5009: cache-hit accounting shares 1 axis with "session_cached_tokens"
    # below (the axis docstring's own "covers two snapshot() dict KEYS
    # instead" note); "ctx_recent_usage" itself is a tuple-shaped value this
    # gate's shape detector does not parse (see module docstring's own
    # disclosed gap) so it never reaches this mapping today.
    "session_cached_tokens": "cache_usage_reported",
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
    dict literal it resolves to could not be found."""
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

    return_node = None
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return):
            return_node = node
            break
    if return_node is None:
        return None, (
            f"check_remote_snapshot_placeholder_declared: {func_name} has no "
            f"return statement -- update this gate."
        )

    if isinstance(return_node.value, ast.Dict):
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


def main() -> int:
    violations = find_violations(_PACKAGE_DIR)
    wire_key_violations = find_wire_keys_violations(_AGUI_STATE_PATH)
    if violations or wire_key_violations:
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
        return 1
    print("check_remote_snapshot_placeholder_declared: OK, every placeholder-shaped "
          "key is covered by _WIRE_KEYS, a declared axis, or a cited exemption, and "
          "_WIRE_KEYS is a verified subset of project_status's unconditional keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
