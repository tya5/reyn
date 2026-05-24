"""Tier 2 invariants for the DROP_SOURCE ToolDefinition (ADR-0033 Phase 1).

Tests cover:
  - Gates: router=allow, phase=allow
  - Parameters schema: source required
  - ToolDefinition identity (name, category, purity)
  - Handler dispatch: IndexDropIROp is built correctly from args
  - Handler passes through op_runtime result

No mocks — uses real ToolDefinition + a monkeypatched execute_op that
captures the dispatched op without touching the index/permission layers.
"""
from __future__ import annotations

import pytest

from reyn.tools.drop_source import DROP_SOURCE
from reyn.tools.types import ToolContext

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_ctx() -> ToolContext:
    """Minimal ToolContext sufficient for handler invocation."""

    class _SentinelEvents:
        subscribers: list = []

    class _SentinelWorkspace:
        pass

    return ToolContext(
        events=_SentinelEvents(),
        permission_resolver=None,
        workspace=_SentinelWorkspace(),
        caller_kind="router",
    )


# ── 1. Identity ───────────────────────────────────────────────────────────────


def test_drop_source_name():
    """Tier 2: DROP_SOURCE.name is 'drop_source'."""
    assert DROP_SOURCE.name == "drop_source"


def test_drop_source_category():
    """Tier 2: DROP_SOURCE.category is 'io' (destructive write operation)."""
    assert DROP_SOURCE.category == "io"


def test_drop_source_purity():
    """Tier 2: DROP_SOURCE.purity is 'side_effect' (modifies index + manifest)."""
    assert DROP_SOURCE.purity == "side_effect"


# ── 2. Gates ─────────────────────────────────────────────────────────────────


def test_drop_source_gates_router_allow():
    """Tier 2: DROP_SOURCE gates.router=allow (LLM may invoke via function calling)."""
    assert DROP_SOURCE.gates.router == "allow"


def test_drop_source_gates_phase_allow():
    """Tier 2: DROP_SOURCE gates.phase=allow (Phase LLM may include in control_ir)."""
    assert DROP_SOURCE.gates.phase == "allow"


def test_drop_source_in_default_registry_for_router():
    """Tier 2: DROP_SOURCE appears in get_default_registry().for_router()."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    names = [t.name for t in registry.for_router()]
    assert "drop_source" in names


def test_drop_source_in_default_registry_for_phase():
    """Tier 2: DROP_SOURCE appears in get_default_registry().for_phase()."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    names = [t.name for t in registry.for_phase()]
    assert "drop_source" in names


# ── 3. Parameters schema ──────────────────────────────────────────────────────


def test_drop_source_parameters_required_source():
    """Tier 2: DROP_SOURCE parameters schema requires 'source'."""
    required = DROP_SOURCE.parameters.get("required", [])
    assert "source" in required


def test_drop_source_parameters_source_is_string():
    """Tier 2: DROP_SOURCE 'source' parameter is typed as string."""
    props = DROP_SOURCE.parameters.get("properties", {})
    assert "source" in props
    assert props["source"]["type"] == "string"


def test_drop_source_render_for_router_shape():
    """Tier 2: render_for_router() has correct function.name and required fields."""
    rendered = DROP_SOURCE.render_for_router()
    assert rendered["type"] == "function"
    fn = rendered["function"]
    assert fn["name"] == "drop_source"
    assert "source" in fn["parameters"]["required"]


def test_drop_source_render_for_phase_shape():
    """Tier 2: render_for_phase() has kind='drop_source' and purity='side_effect'."""
    rendered = DROP_SOURCE.render_for_phase()
    assert rendered["kind"] == "drop_source"
    assert rendered["purity"] == "side_effect"


# ── 4. Handler dispatch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drop_source_handler_builds_index_drop_ir_op(monkeypatch):
    """Tier 2: DROP_SOURCE handler constructs an IndexDropIROp with correct
    source field and dispatches via execute_op."""
    from reyn.schemas.models import IndexDropIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx, *, caller):
        captured_ops.append(op)
        return {"removed": True, "chunks_dropped": 42}

    import reyn.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {"source": "my_source"}
    result = await DROP_SOURCE.handler(args, ctx)

    (op,) = captured_ops
    assert isinstance(op, IndexDropIROp)
    assert op.kind == "index_drop"
    assert op.source == "my_source"

    # Result is passed through from execute_op
    assert result["removed"] is True
    assert result["chunks_dropped"] == 42


@pytest.mark.asyncio
async def test_drop_source_handler_different_source(monkeypatch):
    """Tier 2: DROP_SOURCE handler uses the exact source string from args."""
    from reyn.schemas.models import IndexDropIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx, *, caller):
        captured_ops.append(op)
        return {"removed": False, "chunks_dropped": 0}

    import reyn.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {"source": "reyn_code"}
    await DROP_SOURCE.handler(args, ctx)

    assert captured_ops[0].source == "reyn_code"


@pytest.mark.asyncio
async def test_drop_source_handler_returns_op_result(monkeypatch):
    """Tier 2: DROP_SOURCE handler returns the dict from execute_op unchanged."""
    async def fake_execute_op(op, ctx, *, caller):
        return {"removed": True, "chunks_dropped": 99}

    import reyn.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    result = await DROP_SOURCE.handler({"source": "x"}, ctx)
    assert result["chunks_dropped"] == 99
