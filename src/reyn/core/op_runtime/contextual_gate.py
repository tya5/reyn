"""Contextual capability gate for control-IR ops (#1912b).

The chat / phase RouterLoop tool gate (``router_loop._excluded_result``) and this
control-IR op gate both call the SAME shared check
(``effective.tool_contextually_denied``) — so a per-session contextual narrowing
is enforced on every tool path, bypass-impossible by construction (#1912).

A contextual ``tool_deny`` is expressed in *tool* names (the chat vocabulary,
e.g. ``exec__sandboxed_exec``). A control-IR op has an *op kind* (e.g.
``sandboxed_exec``). This module bridges the two: for an op kind it returns the
contextual name-candidates = ``{kind}`` ∪ the chat-tool qualified aliases. The op
kind itself is ALWAYS a candidate, so an un-aliased kind still gates on its own
name — no op kind can silently bypass. ``_OP_KIND_ALIASES`` is exhaustive over
``ALL_OP_KINDS`` (pinned by ``test_contextual_op_gate_completeness_1912``).
"""
from __future__ import annotations

from reyn.security.permissions.effective import tool_contextually_denied

# op kind → the chat-tool qualified aliases a contextual deny-set may use for it
# (from the universal_dispatch _DISPATCH map). Empty when the op has no distinct
# chat-tool qualified name (it is gated on its own kind name). Must cover every
# entry of ``ALL_OP_KINDS`` — a missing entry would be a silent bypass.
_OP_KIND_ALIASES: "dict[str, frozenset[str]]" = {
    # file ops (file__* → fine-grained op kinds)
    "read_file": frozenset({"file__read"}),
    "write_file": frozenset({"file__write"}),
    "delete_file": frozenset({"file__delete"}),
    "edit_file": frozenset({"file__edit"}),
    "glob_files": frozenset({"file__glob"}),
    "grep_files": frozenset({"file__grep"}),
    # web
    "web_search": frozenset({"web__search"}),
    "web_fetch": frozenset({"web__fetch"}),
    # rag / memory-read
    "recall": frozenset({"rag_operation__recall"}),
    "index_query": frozenset(),
    "index_drop": frozenset({"rag_operation__drop_source"}),
    # exec (the dangerous one — both forms)
    "sandboxed_exec": frozenset({"exec__sandboxed_exec"}),
    # validation
    "lint": frozenset({"validation__lint"}),
    # mcp: the install surface is its OWN op kind (precisely gated); the generic
    # ``mcp`` op (call_tool / list / …) is gated on its kind name (per-verb deny
    # is a follow-up — the built-in untrusted profile denies install, not call).
    "mcp_install": frozenset({
        "mcp__install_registry", "mcp__install_package", "mcp__install_local",
    }),
    "mcp": frozenset(),
    # control-IR-only ops with no distinct chat-tool qualified name → kind only.
    "run_skill": frozenset(),
    "skill_resolve": frozenset(),
    "judge_output": frozenset(),
    "compact": frozenset(),
    "ask_user": frozenset(),
    # #1953 slice 1: Task ops have no distinct chat-tool qualified name → each
    # is gated on its own kind name (no silent bypass).
    "task.create": frozenset(),
    "task.update_status": frozenset(),
    "task.get": frozenset(),
    "task.list": frozenset(),
    "task.add_dependency": frozenset(),
    "task.abort": frozenset(),
    "task.heartbeat": frozenset(),
    "task.register_unblock_predicate": frozenset(),
    "task.comment": frozenset(),
}


def op_kind_tool_names(op_kind: str) -> "frozenset[str]":
    """The contextual name-candidates for a control-IR op kind: the kind itself
    plus its chat-tool qualified aliases."""
    return frozenset({op_kind}) | _OP_KIND_ALIASES.get(op_kind, frozenset())


def op_contextually_denied(contextual: "object | None", op_kind: str) -> bool:
    """True iff the per-session contextual narrowing denies this control-IR op
    (by any of its name candidates). Shares the RouterLoop path's check
    (``tool_contextually_denied``) so enforcement is a single seam."""
    return any(
        tool_contextually_denied(contextual, name)
        for name in op_kind_tool_names(op_kind)
    )


def contextual_denied_result(op_kind: str) -> dict:
    """The decision-enabling denied result for an op blocked by the contextual
    gate — one shared shape across every op-dispatch site (control-IR execute +
    preprocessor run_op / iterate), so a narrowed agent sees the same signal."""
    return {
        "kind": op_kind,
        "status": "denied",
        "error": {
            "kind": "tool_excluded",
            "message": (
                f"op {op_kind!r} is excluded this session by the active "
                "capability profile; it is not available."
            ),
        },
    }
