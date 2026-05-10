"""Tier 2 invariants for the RECALL ToolDefinition (ADR-0033 Phase 1).

Tests cover:
  - Gates: router=allow, phase=allow
  - Parameters schema: query + sources required; top_k / filters / embedding_model optional
  - ToolDefinition identity (name, category, purity)
  - Handler dispatch: RecallIROp is built correctly from args
  - Handler returns op_runtime result dict

No mocks — uses real ToolDefinition + a minimal FakeOpRuntime that captures
the dispatched op without touching the embedding / index layers.
"""
from __future__ import annotations

import pytest

from reyn.tools.recall import RECALL
from reyn.tools.types import ToolContext, ToolGates


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


def test_recall_name():
    """Tier 2: RECALL.name is 'recall'."""
    assert RECALL.name == "recall"


def test_recall_category():
    """Tier 2: RECALL.category is 'discovery' (read-only retrieval)."""
    assert RECALL.category == "discovery"


def test_recall_purity():
    """Tier 2: RECALL.purity is 'read_only' (no workspace writes)."""
    assert RECALL.purity == "read_only"


# ── 2. Gates ─────────────────────────────────────────────────────────────────


def test_recall_gates_router_allow():
    """Tier 2: RECALL gates.router=allow (LLM may invoke via function calling)."""
    assert RECALL.gates.router == "allow"


def test_recall_gates_phase_allow():
    """Tier 2: RECALL gates.phase=allow (Phase LLM may include in control_ir)."""
    assert RECALL.gates.phase == "allow"


def test_recall_in_default_registry_for_router():
    """Tier 2: RECALL appears in get_default_registry().for_router()."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    names = [t.name for t in registry.for_router()]
    assert "recall" in names


def test_recall_in_default_registry_for_phase():
    """Tier 2: RECALL appears in get_default_registry().for_phase()."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    names = [t.name for t in registry.for_phase()]
    assert "recall" in names


# ── 3. Parameters schema ──────────────────────────────────────────────────────


def test_recall_parameters_required_fields():
    """Tier 2: RECALL parameters schema requires 'query' and 'sources'."""
    required = RECALL.parameters.get("required", [])
    assert "query" in required
    assert "sources" in required


def test_recall_parameters_optional_fields_present():
    """Tier 2: RECALL parameters has top_k, filters, embedding_model as optional."""
    props = RECALL.parameters.get("properties", {})
    assert "top_k" in props
    assert "filters" in props
    assert "embedding_model" in props


def test_recall_parameters_sources_is_array():
    """Tier 2: RECALL 'sources' parameter is typed as array of strings."""
    props = RECALL.parameters.get("properties", {})
    assert props["sources"]["type"] == "array"
    assert props["sources"]["items"]["type"] == "string"


def test_recall_parameters_query_is_string():
    """Tier 2: RECALL 'query' parameter is typed as string."""
    props = RECALL.parameters.get("properties", {})
    assert props["query"]["type"] == "string"


def test_recall_render_for_router_shape():
    """Tier 2: render_for_router() has correct function.name and required fields."""
    rendered = RECALL.render_for_router()
    assert rendered["type"] == "function"
    fn = rendered["function"]
    assert fn["name"] == "recall"
    assert "query" in fn["parameters"]["required"]
    assert "sources" in fn["parameters"]["required"]


def test_recall_render_for_phase_shape():
    """Tier 2: render_for_phase() has kind='recall' and purity='read_only'."""
    rendered = RECALL.render_for_phase()
    assert rendered["kind"] == "recall"
    assert rendered["purity"] == "read_only"


# ── 4. Handler dispatch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_handler_builds_recall_ir_op(monkeypatch):
    """Tier 2: RECALL handler constructs a RecallIROp with correct fields and
    dispatches via execute_op."""
    from reyn.schemas.models import RecallIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx, *, caller):
        captured_ops.append(op)
        return {"chunks": [], "mode": "fallback"}

    monkeypatch.setattr("reyn.tools.recall.execute_op", fake_execute_op, raising=False)

    # Patch the import inside the handler (lazy import path)
    import reyn.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {
        "query": "how does auth work",
        "sources": ["reyn_code", "reyn_docs"],
        "top_k": 3,
        "filters": {"source_path": "src/auth.py"},
        "embedding_model": "strong",
    }
    result = await RECALL.handler(args, ctx)

    assert len(captured_ops) == 1
    op = captured_ops[0]
    assert isinstance(op, RecallIROp)
    assert op.kind == "recall"
    assert op.query == "how does auth work"
    assert op.sources == ["reyn_code", "reyn_docs"]
    assert op.top_k == 3
    assert op.filters == {"source_path": "src/auth.py"}
    assert op.embedding_model == "strong"

    # Result is passed through from execute_op
    assert result["mode"] == "fallback"


@pytest.mark.asyncio
async def test_recall_handler_defaults(monkeypatch):
    """Tier 2: RECALL handler uses default top_k=5 and empty filters when not supplied."""
    from reyn.schemas.models import RecallIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx, *, caller):
        captured_ops.append(op)
        return {"chunks": [], "mode": "fallback"}

    import reyn.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {"query": "test query", "sources": ["my_source"]}
    await RECALL.handler(args, ctx)

    assert len(captured_ops) == 1
    op = captured_ops[0]
    assert isinstance(op, RecallIROp)
    assert op.top_k == 5
    assert op.filters == {}
    assert op.embedding_model == "standard"
