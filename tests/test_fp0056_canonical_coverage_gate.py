"""Tier 1: registry-derived canonical-coverage gate (FP-0056 PR-F1).

The load-bearing anti-regression: walk EVERY registered LLM-invocable producer — every op-runtime op
kind AND every router ``ToolDefinition`` — and assert each carries an explicit canonical declaration
(a mapper, the named ``STRUCTURED_PASSTHROUGH`` opt-in, or the provisional ``CANONICAL_TODO`` marker).
Because it enumerates from the *registries* (not a hand-written table), it catches design-level
omissions a hand table misses — it WOULD have caught the ``file`` gap that caused the 2026-07-09
dogfood offload incident.

The RATCHET (this module's second job): ``CANONICAL_TODO`` must not become a permanent escape hatch
("declare it instead of a real mapper → CI green" would resurrect the silent gap under a green CI).
``_CANONICAL_TODO_GRANDFATHERED`` is the exact ledger of the producers relabeled at F1 migration; the
gate asserts the live ``CANONICAL_TODO`` set EQUALS it — so a NEW producer cannot adopt the marker
(real mapper or STRUCTURED_PASSTHROUGH only) without a deliberate, review-gated edit to the ledger, and
a burn-down (mapping a grandfathered producer) must remove its entry (the set is monotonically
non-increasing). Burn-down tracked in **issue #2681**.

Also asserts the failure modes are loud, not silent (``UNDECLARED`` fails the predicate + the seam).
Identity-dispatch (``source=`` resolving a kind-less result) and behavior-preserving migration live in
the sibling test modules.
"""
from __future__ import annotations

import pytest

import reyn.core.op_runtime as op_runtime
from reyn.core.offload.canonical import (
    CANONICAL_TODO,
    STRUCTURED_PASSTHROUGH,
    UNDECLARED,
    canonical_declaration,
    declare_canonical,
)
from reyn.tools import get_default_registry
from reyn.tools.types import ToolDefinition, ToolGates

# The admin/install family whose whole-dict result IS the reviewed, legitimate LLM view
# (FP-0056 owner decision #1). STRUCTURED_PASSTHROUGH membership is EXACTLY this set — no more.
_STRUCTURED_PASSTHROUGH_ADMIN_6 = frozenset({
    "mcp_install",
    "mcp_drop_server",
    "skill_install",
    "pipeline_install",
    "mcp_subscribe_resource",
    "mcp_unsubscribe_resource",
})


# RATCHET LEDGER (issue #2681). The producers relabeled ``CANONICAL_TODO`` at F1 migration — declared
# (gate-satisfying, not a silent gap) but pending a real mapper. This frozenset is the ONLY membership
# the gate permits for ``CANONICAL_TODO``:
#   - Adding a producer here = deliberately incurring debt — a review-gated edit, never a silent
#     "slap CANONICAL_TODO to pass CI" (a NEW producer must ship a real mapper or STRUCTURED_PASSTHROUGH).
#   - Removing an entry = burn-down (a real mapper was written) — the set only ever shrinks.
# Two of the migration's ~54 provisional producers were triaged as text-shaped and given REAL mappers
# in F1 (``read_memory_body`` → the memory body text; ``ask_user`` → the user's answer), so they are
# NOT here. See PR-F1 description for the full text-shaped / genuinely-structured / status-only triage.
#
# #2681 Bucket C burn-down: 25 status-only producers (write/ack/spawn-ack results, no readable body)
# now ship a real ``make_status_text_mapper`` mapper (canonical.py) instead of the whole-dict
# fallback — removed from the ledger. ``topology_create`` was triaged alongside them but its result
# is a genuine RECORD (full topology config echo: kind/members/leader/profiles), not an ack, so it
# stays here for Bucket B.
_CANONICAL_TODO_GRANDFATHERED = frozenset({
    "cron_list",
    "describe_action", "describe_agent", "describe_mcp_tool",
    "invoke_action",
    "list_actions", "list_agents",
    "list_mcp_prompts", "list_mcp_resource_templates", "list_mcp_resources",
    "list_mcp_servers", "list_mcp_tools",
    "list_memory",
    "mcp_search_registry",
    # #2692 burn-down: ``present`` now ships a real text mapper (its ack is an agent-facing
    # signal, not bulk content) — removed from the ledger (the ratchet set only shrinks).
    "search_actions",
    # #2681 Bucket A burn-down: ``shell`` now ships a real text mapper (its stdout is the readable
    # LLM body, mirroring ``sandboxed_exec``'s stdout-is-text treatment) — removed from the ledger
    # (the ratchet set only shrinks).
    "task.abort", "task.add_dependency", "task.assign", "task.create",
    "task.get", "task.list",
    "task.remove_dependency", "task.repoint_dependency", "task.update_status",
    "topology_create",
})


def _is_valid_declaration(decl: object) -> bool:
    """A canonical declaration is the reviewed passthrough opt-in, the provisional TODO marker, or a
    callable mapper — all three satisfy "declared" (an ``UNDECLARED`` producer is what the gate rejects)."""
    return decl is STRUCTURED_PASSTHROUGH or decl is CANONICAL_TODO or callable(decl)


# Enumerate from the live registries at collection time, so a newly registered producer without a
# declaration turns into a RED, individually-named parametrized case (not a silent skip). A tool whose
# name duplicates an op kind (e.g. the admin ``mcp_install`` op + its tool) shares one source id.
_OP_KINDS = sorted(op_runtime.available_kinds())
_TOOLS = sorted(get_default_registry(), key=lambda t: t.name)


def _all_source_ids() -> "list[str]":
    seen: dict[str, None] = {}
    for k in _OP_KINDS:
        seen.setdefault(k, None)
    for t in _TOOLS:
        seen.setdefault(t.name, None)
    return list(seen)


@pytest.mark.parametrize("op_kind", _OP_KINDS)
def test_every_op_kind_has_a_canonical_declaration(op_kind: str) -> None:
    """Tier 1: every registered op-runtime op kind declares a canonical mapping (born at
    ``op_runtime.register(kind, handler, canonical=…)``)."""
    decl = canonical_declaration(op_kind)
    assert decl is not None, f"op kind {op_kind!r} has no canonical declaration"
    assert _is_valid_declaration(decl), f"op kind {op_kind!r}: {decl!r} is not a valid declaration"


@pytest.mark.parametrize("tool", _TOOLS, ids=[t.name for t in _TOOLS])
def test_every_tool_definition_has_a_canonical_declaration(tool: ToolDefinition) -> None:
    """Tier 1: every router ToolDefinition carries a canonical declaration (its ``canonical`` field),
    and it resolves by invoked identity (tool name) through the canonical registry."""
    assert tool.canonical is not UNDECLARED, f"ToolDefinition {tool.name!r} is UNDECLARED"
    assert _is_valid_declaration(tool.canonical), f"{tool.name!r}: {tool.canonical!r} invalid"
    assert canonical_declaration(tool.name) is tool.canonical, (
        f"{tool.name!r} did not register its declaration into the canonical registry"
    )


def test_canonical_todo_is_ratcheted_to_the_grandfather_ledger() -> None:
    """Tier 1: the RATCHET — the live ``CANONICAL_TODO`` set EQUALS ``_CANONICAL_TODO_GRANDFATHERED``.

    ``!=`` fires on either escape: a NEW producer adopting ``CANONICAL_TODO`` (not in the ledger →
    must ship a real mapper / STRUCTURED_PASSTHROUGH instead), OR a burn-down that mapped a
    grandfathered producer without removing its stale ledger entry. Either way the marker cannot
    silently become a permanent alternative to a real mapper (issue #2681)."""
    live_todo = {
        sid for sid in _all_source_ids() if canonical_declaration(sid) is CANONICAL_TODO
    }
    assert live_todo == set(_CANONICAL_TODO_GRANDFATHERED), (
        "CANONICAL_TODO membership drifted from the ratchet ledger — a new producer may not adopt the "
        "marker (write a real mapper or STRUCTURED_PASSTHROUGH), and a burn-down must delete its "
        f"ledger entry. Added: {live_todo - set(_CANONICAL_TODO_GRANDFATHERED)}; "
        f"stale (mapped but still listed): {set(_CANONICAL_TODO_GRANDFATHERED) - live_todo}"
    )


def test_structured_passthrough_membership_is_exactly_the_admin_6() -> None:
    """Tier 1: ``STRUCTURED_PASSTHROUGH`` is declared for EXACTLY the admin/install family (owner
    decision #1) — no more. A non-admin producer that wants whole-dict output uses ``CANONICAL_TODO``
    (a distinct, tracked marker), so a reader can tell "reviewed-legitimate" from "todo"."""
    live_passthrough = {
        sid for sid in _all_source_ids() if canonical_declaration(sid) is STRUCTURED_PASSTHROUGH
    }
    assert live_passthrough == set(_STRUCTURED_PASSTHROUGH_ADMIN_6), (
        f"STRUCTURED_PASSTHROUGH must be exactly the admin-6; got {sorted(live_passthrough)}"
    )


def test_gate_predicate_rejects_an_undeclared_fixture_tool() -> None:
    """Tier 1: (falsify) a ToolDefinition left with the default ``UNDECLARED`` canonical is what the
    gate rejects — proving the gate would fire on a real omission, not vacuously pass."""

    async def _noop(args, ctx):  # pragma: no cover - never invoked
        return {}

    undeclared = ToolDefinition(
        name="fixture_undeclared_producer",
        description="a producer that forgot to declare its canonical shape",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(),
        handler=_noop,
        category="test",
    )
    # The default is UNDECLARED, and the gate predicate rejects it.
    assert undeclared.canonical is UNDECLARED
    assert not _is_valid_declaration(undeclared.canonical)


def test_declare_canonical_refuses_undeclared() -> None:
    """Tier 1: (falsify) the seam itself refuses to record UNDECLARED — omission cannot slip into the
    registry as a silent no-op."""
    with pytest.raises(ValueError):
        declare_canonical("fixture_undeclared_source", UNDECLARED)
