"""Tier 2 invariants for the SEMANTIC_SEARCH ToolDefinition (ADR-0033 Phase 1;
FP-0057 Phase 2a renamed from RECALL — clean break, fixes the observed
recall/search_actions/memory naming collision).

Tests cover:
  - Gates: router=allow, phase=allow
  - Parameters schema: query + sources required; top_k / filters / embedding_model optional
  - ToolDefinition identity (name, category, purity)
  - Handler dispatch: SemanticSearchIROp is built correctly from args
  - Handler returns op_runtime result dict

No mocks — uses real ToolDefinition + a minimal FakeOpRuntime that captures
the dispatched op without touching the embedding / index layers.
"""
from __future__ import annotations

import pytest

from reyn.tools.semantic_search import SEMANTIC_SEARCH
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
    """Tier 2: SEMANTIC_SEARCH.name is 'semantic_search'."""
    assert SEMANTIC_SEARCH.name == "semantic_search"


def test_recall_category():
    """Tier 2: SEMANTIC_SEARCH.category is 'discovery' (read-only retrieval)."""
    assert SEMANTIC_SEARCH.category == "discovery"


def test_recall_purity():
    """Tier 2: SEMANTIC_SEARCH.purity is 'read_only' (no workspace writes)."""
    assert SEMANTIC_SEARCH.purity == "read_only"


# ── 2. Gates ─────────────────────────────────────────────────────────────────


def test_recall_gates_router_allow():
    """Tier 2: SEMANTIC_SEARCH gates.router=allow (LLM may invoke via function calling)."""
    assert SEMANTIC_SEARCH.gates.router == "allow"


def test_recall_gates_phase_allow():
    """Tier 2: SEMANTIC_SEARCH gates.phase=allow (Phase LLM may include in control_ir)."""
    assert SEMANTIC_SEARCH.gates.phase == "allow"


def test_recall_in_default_registry_for_router():
    """Tier 2: SEMANTIC_SEARCH appears in get_default_registry().for_router()."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    names = [t.name for t in registry.for_router()]
    assert "semantic_search" in names


def test_recall_in_default_registry_for_phase():
    """Tier 2: SEMANTIC_SEARCH appears in get_default_registry().for_phase()."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    names = [t.name for t in registry.for_phase()]
    assert "semantic_search" in names


# ── 3. Parameters schema ──────────────────────────────────────────────────────


def test_recall_parameters_required_fields():
    """Tier 2: SEMANTIC_SEARCH parameters schema requires 'query' and 'sources'."""
    required = SEMANTIC_SEARCH.parameters.get("required", [])
    assert "query" in required
    assert "sources" in required


def test_recall_parameters_optional_fields_present():
    """Tier 2: SEMANTIC_SEARCH parameters has top_k, filters, embedding_model as optional."""
    props = SEMANTIC_SEARCH.parameters.get("properties", {})
    assert "top_k" in props
    assert "filters" in props
    assert "embedding_model" in props


def test_recall_parameters_sources_is_array():
    """Tier 2: SEMANTIC_SEARCH 'sources' parameter is typed as array of strings."""
    props = SEMANTIC_SEARCH.parameters.get("properties", {})
    assert props["sources"]["type"] == "array"
    assert props["sources"]["items"]["type"] == "string"


def test_recall_parameters_query_is_string():
    """Tier 2: SEMANTIC_SEARCH 'query' parameter is typed as string."""
    props = SEMANTIC_SEARCH.parameters.get("properties", {})
    assert props["query"]["type"] == "string"


def test_recall_render_for_router_shape():
    """Tier 2: render_for_router() has correct function.name and required fields."""
    rendered = SEMANTIC_SEARCH.render_for_router()
    assert rendered["type"] == "function"
    fn = rendered["function"]
    assert fn["name"] == "semantic_search"
    assert "query" in fn["parameters"]["required"]
    assert "sources" in fn["parameters"]["required"]


def test_recall_render_for_phase_shape():
    """Tier 2: render_for_phase() has kind='semantic_search' and purity='read_only'."""
    rendered = SEMANTIC_SEARCH.render_for_phase()
    assert rendered["kind"] == "semantic_search"
    assert rendered["purity"] == "read_only"


# ── 4. Handler dispatch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_handler_builds_recall_ir_op(monkeypatch):
    """Tier 2: SEMANTIC_SEARCH handler constructs a SemanticSearchIROp with
    correct fields and dispatches via execute_op."""
    from reyn.schemas.models import SemanticSearchIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx):
        captured_ops.append(op)
        return {"chunks": [], "mode": "fallback"}

    monkeypatch.setattr("reyn.tools.semantic_search.execute_op", fake_execute_op, raising=False)

    # Patch the import inside the handler (lazy import path)
    import reyn.core.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {
        "query": "how does auth work",
        "sources": ["reyn_code", "reyn_docs"],
        "top_k": 3,
        "filters": {"source_path": "src/auth.py"},
        "embedding_model": "strong",
    }
    result = await SEMANTIC_SEARCH.handler(args, ctx)

    op = captured_ops[0]
    assert isinstance(op, SemanticSearchIROp)
    assert op.kind == "semantic_search"
    assert op.query == "how does auth work"
    assert op.sources == ["reyn_code", "reyn_docs"]
    assert op.top_k == 3
    assert op.filters == {"source_path": "src/auth.py"}
    assert op.embedding_model == "strong"

    # Result is passed through from execute_op
    assert result["mode"] == "fallback"


@pytest.mark.asyncio
async def test_recall_handler_defaults(monkeypatch):
    """Tier 2: SEMANTIC_SEARCH handler uses default top_k=5 and empty filters
    when not supplied."""
    from reyn.schemas.models import SemanticSearchIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx):
        captured_ops.append(op)
        return {"chunks": [], "mode": "fallback"}

    import reyn.core.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {"query": "test query", "sources": ["my_source"]}
    await SEMANTIC_SEARCH.handler(args, ctx)

    op = captured_ops[0]
    assert isinstance(op, SemanticSearchIROp)
    assert op.top_k == 5
    assert op.filters == {}
    assert op.embedding_model == "standard"
