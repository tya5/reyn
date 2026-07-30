"""Contextual capability gate for control-IR ops (#1912b).

The chat / phase RouterLoop tool gate (``router_loop._excluded_result``) and this
control-IR op gate both call the SAME shared check
(``effective.tool_contextually_denied``) — so a per-session contextual narrowing
is enforced on every tool path, bypass-impossible by construction (#1912).

A contextual ``tool_deny`` is expressed in *tool* names (the chat vocabulary,
e.g. ``exec``). A control-IR op has an *op kind* (e.g. ``sandboxed_exec``), and
for most ops the two strings are the same. This module bridges the ones where
they differ: for an op kind it returns the contextual name-candidates =
``{kind}`` ∪ the chat-tool names that reach it. The op kind itself is ALWAYS a
candidate, so an un-named kind still gates on its own name — no op kind can
silently bypass. ``_OP_KIND_TOOLS`` is exhaustive over ``ALL_OP_KINDS`` (pinned
by ``test_contextual_op_gate_completeness_1912``).

#3429 shrank this table by two thirds. Every file / web op used to carry a
qualified alias (``read_file`` → ``file__read``) purely because the chat tool had
a second spelling; with the second spelling gone, the op kind and the tool name
are the same string and those entries are empty. What is left is the three
genuine op-kind ≠ tool-name cases: ``sandboxed_exec``/``exec``, the ONE
``mcp_install`` op behind THREE source-split install tools, and nothing else.
"""
from __future__ import annotations

from reyn.security.permissions.effective import tool_contextually_denied

# op kind → the chat-tool names a contextual deny-set may use for it, when those
# differ from the kind. Empty when the chat tool carries the same name as the op
# kind (it is gated on that one name). Must cover every entry of ``ALL_OP_KINDS``
# — a missing entry would be a silent bypass.
_OP_KIND_TOOLS: "dict[str, frozenset[str]]" = {
    # file ops — the chat tool and the op kind are the same name (#3429), so
    # there is nothing to add: ``read_file`` gates ``read_file``.
    "read_file": frozenset(),
    "write_file": frozenset(),
    "delete_file": frozenset(),
    "edit_file": frozenset(),
    "glob_files": frozenset(),
    "grep_files": frozenset(),
    # web — same-name, as above.
    "web_search": frozenset(),
    "web_fetch": frozenset(),
    # rag / memory-read
    # FP-0057 Phase 1: embed is the raw user-facing embedding primitive — no
    # distinct chat-tool name → gated on its own kind name only,
    # same shape as index_query.
    "embed": frozenset(),
    # FP-0066 P1b: semantic_search / index_drop no longer have a distinct
    # chat-tool name — the layer-1 agent tools that used to expose them are
    # retired (the OS-internal op kind itself is kept; see the retrieval
    # redesign doc §9) →
    # gated on their own kind name only, same shape as index_query/index_update.
    "semantic_search": frozenset(),
    "index_query": frozenset(),
    "index_drop": frozenset(),
    "index_update": frozenset(),
    # exec — a genuine op-kind ≠ tool-name case. #3226 Phase 3 renamed the
    # chat TOOL sandboxed_exec -> exec; the op kind key here is UNCHANGED
    # (op_runtime layer), so both strings must gate it.
    "sandboxed_exec": frozenset({"exec"}),
    # mcp: the install surface is its OWN op kind, reached by three source-split
    # chat tools (registry / package / local) — the second genuine
    # op-kind ≠ tool-name case, and the only one-to-many one. The generic
    # ``mcp`` op (call_tool / list / …) is gated on its kind name (per-verb deny
    # is a follow-up — the built-in untrusted profile denies install, not call).
    "mcp_install": frozenset({
        "mcp_install_registry", "mcp_install_package", "mcp_install_local",
    }),
    "mcp_drop_server": frozenset(),
    "mcp": frozenset(),
    # control-IR-only ops with no distinct chat-tool name → kind only.
    "compact": frozenset(),
    "ask_user": frozenset(),
    # FP-0054 PR-A: present is Tier 0 (no output gate) but still gates on its own
    # kind name under a per-session contextual narrowing (no distinct chat-tool
    # name → kind only; no silent bypass).
    "present": frozenset(),
}


def op_kind_tool_names(op_kind: str) -> "frozenset[str]":
    """The contextual name-candidates for a control-IR op kind: the kind itself
    plus any chat-tool name that differs from it."""
    return frozenset({op_kind}) | _OP_KIND_TOOLS.get(op_kind, frozenset())


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
