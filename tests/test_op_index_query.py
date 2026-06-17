"""Tier 2: index_query op handler OS invariants (ADR-0033 Phase 1).

All tests use real SqliteIndexBackend with tmp_path workspace isolation.
No mocks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.data.index.backend import ChunkRecord
from reyn.data.index.backends.sqlite import SqliteIndexBackend
from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.schemas.models import IndexQueryIROp
from reyn.security.permissions.permissions import PermissionDecl

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
    )


def _make_chunk(
    text: str,
    vector: list[float],
    content_hash: str,
    embedding_model: str = "test-model",
) -> ChunkRecord:
    return ChunkRecord(
        text=text,
        vector=vector,
        metadata={
            "source_path": "file.txt",
            "source_type": "generic",
            "content_hash": content_hash,
            "embedding_model": embedding_model,
            "chunk_index": 0,
            "size_tokens": len(text.split()),
            "parent_context": None,
        },
        score=None,
    )


async def _seed_source(workspace_root: Path, source: str, chunks: list[ChunkRecord]) -> None:
    """Write chunks directly to the index backend for test setup."""
    backend = SqliteIndexBackend(workspace_root=workspace_root)
    await backend.write(source, chunks, mode="append")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_semantic_query_returns_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: query with a vector returns top-K chunks in semantic mode."""
    import os
    monkeypatch.chdir(tmp_path)

    await _seed_source(tmp_path, "src1", [
        _make_chunk("hello", [1.0, 0.0, 0.0], "h1"),
        _make_chunk("world", [0.0, 1.0, 0.0], "h2"),
    ])

    ctx = _make_ctx(tmp_path)
    op = IndexQueryIROp(
        kind="index_query",
        source="src1",
        query_vector=[1.0, 0.0, 0.0],
        top_k=2,
        filters={},
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert result["mode"] == "semantic"
    assert len(result["chunks"]) >= 1
    assert result["chunks"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_query_empty_source_returns_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: querying a source with no chunks returns mode='fallback'."""
    import os
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    op = IndexQueryIROp(
        kind="index_query",
        source="empty_src",
        query_vector=[1.0, 0.0, 0.0],
        top_k=5,
        filters={},
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert result["mode"] == "fallback"
    assert result["chunks"] == []


@pytest.mark.asyncio
async def test_null_query_vector_returns_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: query_vector=None triggers fallback enumerate path (phase 1 = empty)."""
    import os
    monkeypatch.chdir(tmp_path)

    await _seed_source(tmp_path, "src1", [
        _make_chunk("some content", [1.0, 0.0], "hx"),
    ])

    ctx = _make_ctx(tmp_path)
    op = IndexQueryIROp(
        kind="index_query",
        source="src1",
        query_vector=None,
        top_k=5,
        filters={},
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert result["mode"] == "fallback"


@pytest.mark.asyncio
async def test_top_k_limits_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: top_k parameter limits number of returned chunks."""
    import os
    monkeypatch.chdir(tmp_path)

    chunks = [
        _make_chunk(f"chunk {i}", [float(i), 0.0, 0.0], f"h{i}")
        for i in range(10)
    ]
    await _seed_source(tmp_path, "src1", chunks)

    ctx = _make_ctx(tmp_path)
    op = IndexQueryIROp(
        kind="index_query",
        source="src1",
        query_vector=[1.0, 0.0, 0.0],
        top_k=3,
        filters={},
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert not result["chunks"][3:]  # top_k=3 caps result count


@pytest.mark.asyncio
async def test_filters_narrow_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: filters narrow results to chunks matching the filter field."""
    import os
    monkeypatch.chdir(tmp_path)

    # Two chunks with different source_type
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    chunk_a = ChunkRecord(
        text="python file",
        vector=[1.0, 0.0, 0.0],
        metadata={
            "source_path": "a.py",
            "source_type": "python",
            "content_hash": "pa",
            "embedding_model": "m",
            "chunk_index": 0,
            "size_tokens": 2,
            "parent_context": None,
        },
        score=None,
    )
    chunk_b = ChunkRecord(
        text="markdown file",
        vector=[1.0, 0.0, 0.0],
        metadata={
            "source_path": "b.md",
            "source_type": "markdown",
            "content_hash": "pb",
            "embedding_model": "m",
            "chunk_index": 0,
            "size_tokens": 2,
            "parent_context": None,
        },
        score=None,
    )
    await backend.write("src1", [chunk_a, chunk_b], mode="append")

    ctx = _make_ctx(tmp_path)
    op = IndexQueryIROp(
        kind="index_query",
        source="src1",
        query_vector=[1.0, 0.0, 0.0],
        top_k=10,
        filters={"source_type": "python"},
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert all(c["metadata"]["source_type"] == "python" for c in result["chunks"])
