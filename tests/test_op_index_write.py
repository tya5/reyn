"""Tier 2: index_write op handler OS invariants (ADR-0033 Phase 1).

All tests use real SqliteIndexBackend + real SourceManifest with tmp_path
workspace isolation. No mocks.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reyn.events.events import EventLog
from reyn.index.backends.sqlite import SqliteIndexBackend
from reyn.index.source_manifest import get_source_manifest
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.permissions.permissions import PermissionDecl
from reyn.schemas.models import IndexWriteIROp
from reyn.workspace.workspace import Workspace

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


def _inline_chunk(
    text: str,
    vector: list[float],
    content_hash: str,
    embedding_model: str = "test-model",
) -> dict[str, Any]:
    return {
        "text": text,
        "vector": vector,
        "metadata": {
            "source_path": "file.txt",
            "source_type": "generic",
            "content_hash": content_hash,
            "embedding_model": embedding_model,
            "chunk_index": 0,
            "size_tokens": len(text.split()),
        },
    }


def _write_jsonl(path: Path, chunks: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk) + "\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inline_write_roundtrip(tmp_path: Path) -> None:
    """Tier 2: Form A inline chunks write to backend and return written count."""
    monkeypatch_cwd = tmp_path
    import os
    orig = os.getcwd()
    os.chdir(tmp_path)
    try:
        ctx = _make_ctx(tmp_path)
        chunks = [
            _inline_chunk("hello", [1.0, 0.0, 0.0], "h1"),
            _inline_chunk("world", [0.0, 1.0, 0.0], "h2"),
        ]
        op = IndexWriteIROp(
            kind="index_write",
            source="test_src",
            chunks=chunks,
            mode="append",
        )
        result = await execute_op(op, ctx, caller="control_ir")

        assert result.get("status") != "error", result
        assert result["written"] == 2
        assert result["skipped"] == 0

        # Verify manifest was updated
        manifest = get_source_manifest(tmp_path)
        entry = await manifest.get("test_src")
        assert entry is not None
        assert entry.chunk_count == 2
    finally:
        os.chdir(orig)


@pytest.mark.asyncio
async def test_artifact_form_write(tmp_path: Path) -> None:
    """Tier 2: Form B artifact reference reads JSONL and writes to backend."""
    import os
    os.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    chunks = [
        _inline_chunk("chunk one", [1.0, 0.0], "h1"),
        _inline_chunk("chunk two", [0.0, 1.0], "h2"),
    ]
    input_path = tmp_path / "artifacts" / "chunks_with_vectors.jsonl"
    _write_jsonl(input_path, chunks)

    op = IndexWriteIROp(
        kind="index_write",
        source="test_src",
        input_artifact="artifacts/chunks_with_vectors.jsonl",
        mode="append",
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert result["written"] == 2
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_dedup_by_content_hash(tmp_path: Path) -> None:
    """Tier 2: writing the same content_hash twice skips the second insert."""
    import os
    os.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    chunk = _inline_chunk("duplicate", [1.0, 0.0], "dup-hash")
    op = IndexWriteIROp(
        kind="index_write",
        source="test_src",
        chunks=[chunk],
        mode="append",
    )

    r1 = await execute_op(op, ctx, caller="control_ir")
    r2 = await execute_op(op, ctx, caller="control_ir")

    assert r1["written"] == 1
    assert r2["written"] == 0
    assert r2["skipped"] == 1


@pytest.mark.asyncio
async def test_replace_mode_clears_existing(tmp_path: Path) -> None:
    """Tier 2: mode='replace' removes old chunks before writing new ones."""
    import os
    os.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    chunk_a = _inline_chunk("old content", [1.0, 0.0, 0.0], "hash-old")
    op_append = IndexWriteIROp(
        kind="index_write",
        source="test_src",
        chunks=[chunk_a],
        mode="append",
    )
    await execute_op(op_append, ctx, caller="control_ir")

    chunk_b = _inline_chunk("new content", [0.0, 1.0, 0.0], "hash-new")
    op_replace = IndexWriteIROp(
        kind="index_write",
        source="test_src",
        chunks=[chunk_b],
        mode="replace",
    )
    result = await execute_op(op_replace, ctx, caller="control_ir")
    assert result.get("status") != "error", result

    # Verify only the new chunk exists via backend query
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    hits = await backend.query("test_src", [0.0, 1.0, 0.0], top_k=10, filters={})
    assert len(hits) == 1
    assert hits[0]["text"] == "new content"


@pytest.mark.asyncio
async def test_missing_artifact_returns_error(tmp_path: Path) -> None:
    """Tier 2: Form B with non-existent input_artifact returns error status."""
    import os
    os.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    op = IndexWriteIROp(
        kind="index_write",
        source="test_src",
        input_artifact="artifacts/nonexistent.jsonl",
        mode="append",
    )
    result = await execute_op(op, ctx, caller="control_ir")
    assert result["status"] == "error"
