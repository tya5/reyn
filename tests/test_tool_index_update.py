"""Tier 2 invariants for the INDEX_UPDATE ToolDefinition (FP-0057 Phase 2a).

Tests cover:
  - Gates: router=allow, phase=allow (default-allow, own-write op)
  - Parameters schema: source + chunks required; embedding_model / description / path optional
  - ToolDefinition identity (name, category)
  - Handler dispatch: IndexUpdateIROp is built correctly from args
  - Defensive missing-arg handling (mirrors semantic_search's missing_required_arg shape)

No mocks — uses real ToolDefinition + a minimal FakeOpRuntime that captures
the dispatched op without touching the embedding / index layers.
"""
from __future__ import annotations

import pytest

from reyn.tools.index_update import INDEX_UPDATE
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


def test_index_update_name():
    """Tier 2: INDEX_UPDATE.name is 'index_update'."""
    assert INDEX_UPDATE.name == "index_update"


def test_index_update_category():
    """Tier 2: INDEX_UPDATE.category is 'discovery' (the RAG-op family)."""
    assert INDEX_UPDATE.category == "discovery"


# ── 2. Gates ─────────────────────────────────────────────────────────────────


def test_index_update_gates_router_allow():
    """Tier 2: INDEX_UPDATE gates.router=allow (default-allow, own-write op)."""
    assert INDEX_UPDATE.gates.router == "allow"


def test_index_update_gates_phase_allow():
    """Tier 2: INDEX_UPDATE gates.phase=allow."""
    assert INDEX_UPDATE.gates.phase == "allow"


def test_index_update_in_default_registry_for_router():
    """Tier 2: INDEX_UPDATE appears in get_default_registry().for_router()."""
    from reyn.tools import get_default_registry
    registry = get_default_registry()
    names = [t.name for t in registry.for_router()]
    assert "index_update" in names


# ── 3. Parameters schema ──────────────────────────────────────────────────────


def test_index_update_parameters_required_fields():
    """Tier 2: INDEX_UPDATE parameters schema requires 'source' and 'chunks'."""
    required = INDEX_UPDATE.parameters.get("required", [])
    assert "source" in required
    assert "chunks" in required


def test_index_update_parameters_optional_fields_present():
    """Tier 2: INDEX_UPDATE parameters has embedding_model / description / path as optional."""
    props = INDEX_UPDATE.parameters.get("properties", {})
    assert "embedding_model" in props
    assert "description" in props
    assert "path" in props


def test_index_update_parameters_chunks_is_array():
    """Tier 2: INDEX_UPDATE 'chunks' parameter is typed as an array of objects."""
    props = INDEX_UPDATE.parameters.get("properties", {})
    assert props["chunks"]["type"] == "array"
    assert props["chunks"]["items"]["type"] == "object"


# ── 4. Handler dispatch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_update_handler_builds_index_update_ir_op(monkeypatch):
    """Tier 2: INDEX_UPDATE handler constructs an IndexUpdateIROp with correct
    fields and dispatches via execute_op."""
    from reyn.schemas.models import IndexUpdateIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx):
        captured_ops.append(op)
        return {"kind": "index_update", "added": 1, "updated": 0, "removed": 0, "skipped": 0}

    import reyn.core.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {
        "source": "docs",
        "chunks": [{"text": "hello", "metadata": {"content_hash": "h1", "source_path": "a.md"}}],
        "embedding_model": "strong",
        "description": "my docs",
        "path": "docs/**/*.md",
    }
    result = await INDEX_UPDATE.handler(args, ctx)

    op = captured_ops[0]
    assert isinstance(op, IndexUpdateIROp)
    assert op.kind == "index_update"
    assert op.source == "docs"
    assert op.chunks == args["chunks"]
    assert op.embedding_model == "strong"
    assert op.description == "my docs"
    assert op.path == "docs/**/*.md"
    assert result["added"] == 1


@pytest.mark.asyncio
async def test_index_update_handler_defaults(monkeypatch):
    """Tier 2: INDEX_UPDATE handler uses default embedding_model='standard' and
    None description/path when not supplied."""
    from reyn.schemas.models import IndexUpdateIROp

    captured_ops: list = []

    async def fake_execute_op(op, ctx):
        captured_ops.append(op)
        return {"kind": "index_update"}

    import reyn.core.op_runtime as _orm
    monkeypatch.setattr(_orm, "execute_op", fake_execute_op)

    ctx = _make_ctx()
    args = {
        "source": "docs",
        "chunks": [{"text": "hello", "metadata": {"content_hash": "h1", "source_path": "a.md"}}],
    }
    await INDEX_UPDATE.handler(args, ctx)

    op = captured_ops[0]
    assert isinstance(op, IndexUpdateIROp)
    assert op.embedding_model == "standard"
    assert op.description is None
    assert op.path is None


# ── 5. Defensive missing-arg handling ────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_update_returns_error_when_source_missing():
    """Tier 2: missing 'source' returns a structured error, not a raised KeyError."""
    result = await INDEX_UPDATE.handler({"chunks": [{}]}, _make_ctx())
    assert result["ok"] is False
    assert result["error_kind"] == "missing_required_arg"
    assert "source" in result["missing"]


@pytest.mark.asyncio
async def test_index_update_returns_error_when_chunks_missing():
    """Tier 2: missing 'chunks' returns a structured error, not a raised KeyError."""
    result = await INDEX_UPDATE.handler({"source": "docs"}, _make_ctx())
    assert result["ok"] is False
    assert result["error_kind"] == "missing_required_arg"
    assert "chunks" in result["missing"]
