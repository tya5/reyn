"""Tier 1: registry-derived canonical-coverage gate (FP-0056 PR-F1).

The load-bearing anti-regression: walk EVERY registered LLM-invocable producer — every op-runtime op
kind AND every router ``ToolDefinition`` — and assert each carries an explicit canonical declaration
(a mapper, or the named ``STRUCTURED_PASSTHROUGH`` opt-in). Because it enumerates from the *registries*
(not a hand-written table), it catches design-level omissions a hand table misses — it WOULD have
caught the ``file`` gap that caused the 2026-07-09 dogfood offload incident.

Also asserts the two failure modes are loud, not silent: an ``UNDECLARED`` producer fails the gate
predicate, and ``declare_canonical`` refuses ``UNDECLARED``. Identity-dispatch (``source=`` resolving a
kind-less result) and behavior-preserving migration live in the sibling test modules.
"""
from __future__ import annotations

import pytest

import reyn.core.op_runtime as op_runtime
from reyn.core.offload.canonical import (
    STRUCTURED_PASSTHROUGH,
    UNDECLARED,
    canonical_declaration,
    declare_canonical,
)
from reyn.tools import get_default_registry
from reyn.tools.types import ToolDefinition, ToolGates


def _is_valid_declaration(decl: object) -> bool:
    """A canonical declaration is either the explicit passthrough opt-in or a callable mapper."""
    return decl is STRUCTURED_PASSTHROUGH or callable(decl)


# Enumerate from the live registries at collection time, so a newly registered producer without a
# declaration turns into a RED, individually-named parametrized case (not a silent skip).
_OP_KINDS = sorted(op_runtime.available_kinds())
_TOOLS = sorted(get_default_registry(), key=lambda t: t.name)


@pytest.mark.parametrize("op_kind", _OP_KINDS)
def test_every_op_kind_has_a_canonical_declaration(op_kind: str) -> None:
    """Tier 1: every registered op-runtime op kind declares a canonical mapping (born at
    ``op_runtime.register(kind, handler, canonical=…)``)."""
    decl = canonical_declaration(op_kind)
    assert decl is not None, f"op kind {op_kind!r} has no canonical declaration"
    assert _is_valid_declaration(decl), f"op kind {op_kind!r}: {decl!r} is not a mapper/passthrough"


@pytest.mark.parametrize("tool", _TOOLS, ids=[t.name for t in _TOOLS])
def test_every_tool_definition_has_a_canonical_declaration(tool: ToolDefinition) -> None:
    """Tier 1: every router ToolDefinition carries a canonical declaration (its ``canonical`` field),
    and it resolves by invoked identity (tool name) through the canonical registry."""
    assert tool.canonical is not UNDECLARED, f"ToolDefinition {tool.name!r} is UNDECLARED"
    assert _is_valid_declaration(tool.canonical), f"{tool.name!r}: {tool.canonical!r} invalid"
    assert canonical_declaration(tool.name) is tool.canonical, (
        f"{tool.name!r} did not register its declaration into the canonical registry"
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


def test_structured_passthrough_membership_matches_owner_decision() -> None:
    """Tier 1: the admin/install op family declared STRUCTURED_PASSTHROUGH per FP-0056 decision #1
    (owner-confirmed) — the whole-dict result legitimately IS the LLM view for these."""
    admin_ops = [
        "mcp_install",
        "mcp_drop_server",
        "skill_install",
        "pipeline_install",
        "mcp_subscribe_resource",
        "mcp_unsubscribe_resource",
    ]
    for kind in admin_ops:
        assert canonical_declaration(kind) is STRUCTURED_PASSTHROUGH, (
            f"{kind!r} should be a reviewed STRUCTURED_PASSTHROUGH (decision #1)"
        )
