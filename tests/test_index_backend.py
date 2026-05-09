"""Tier 2 tests: IndexBackend OS invariants (ADR-0033 Phase 1).

All tests use real SqliteIndexBackend instances with tmp_path workspace
isolation. No mocks.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from reyn.index.backend import ChunkRecord
from reyn.index.backends.sqlite import SqliteIndexBackend

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_chunk(
    text: str,
    vector: list[float],
    content_hash: str,
    source_path: str = "file.txt",
    source_type: str = "generic",
    embedding_model: str = "test-model",
    chunk_index: int = 0,
) -> ChunkRecord:
    return ChunkRecord(
        text=text,
        vector=vector,
        metadata={
            "source_path": source_path,
            "source_type": source_type,
            "content_hash": content_hash,
            "embedding_model": embedding_model,
            "chunk_index": chunk_index,
            "size_tokens": len(text.split()),
            "parent_context": None,
        },
        score=None,
    )


def _unit_vec(dim: int, hot: int) -> list[float]:
    """One-hot unit vector of dimension `dim` with index `hot` set to 1.0."""
    v = [0.0] * dim
    v[hot] = 1.0
    return v


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_query_roundtrip(tmp_path: Path) -> None:
    """Tier 2: write a single chunk and retrieve it via query roundtrip."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    chunk = _make_chunk("hello world", [1.0, 0.0, 0.0], content_hash="h1")

    result = await backend.write("src1", [chunk], mode="append")
    assert result["written"] == 1
    assert result["skipped"] == 0

    hits = await backend.query("src1", [1.0, 0.0, 0.0], top_k=5, filters={})
    assert len(hits) == 1
    assert hits[0]["text"] == "hello world"
    assert hits[0]["score"] is not None
    assert abs(hits[0]["score"] - 1.0) < 1e-5


@pytest.mark.asyncio
async def test_write_append_dedup_by_content_hash(tmp_path: Path) -> None:
    """Tier 2: writing the same content_hash twice skips the second insert (dedup)."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    chunk = _make_chunk("hello", [1.0, 0.0], content_hash="dup-hash")

    r1 = await backend.write("src1", [chunk], mode="append")
    assert r1["written"] == 1
    assert r1["skipped"] == 0

    r2 = await backend.write("src1", [chunk], mode="append")
    assert r2["written"] == 0
    assert r2["skipped"] == 1

    stat = await backend.stat("src1")
    assert stat["chunk_count"] == 1


@pytest.mark.asyncio
async def test_write_replace_truncates_previous(tmp_path: Path) -> None:
    """Tier 2: replace mode deletes all prior chunks before writing new ones."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)

    old = _make_chunk("old text", [1.0, 0.0], content_hash="old-h")
    await backend.write("src1", [old], mode="append")

    new = _make_chunk("new text", [0.0, 1.0], content_hash="new-h")
    r = await backend.write("src1", [new], mode="replace")
    assert r["written"] == 1

    stat = await backend.stat("src1")
    assert stat["chunk_count"] == 1

    hits = await backend.query("src1", [0.0, 1.0], top_k=5, filters={})
    assert len(hits) == 1
    assert hits[0]["text"] == "new text"


@pytest.mark.asyncio
async def test_query_topk_by_cosine_similarity(tmp_path: Path) -> None:
    """Tier 2: query returns results sorted descending by cosine similarity."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    dim = 4

    # chunk A: perfectly aligned with query
    chunk_a = _make_chunk("most similar", _unit_vec(dim, 0), content_hash="ca")
    # chunk B: orthogonal to query (similarity = 0)
    chunk_b = _make_chunk("orthogonal", _unit_vec(dim, 1), content_hash="cb")
    # chunk C: partially aligned (45 degree angle, similarity ≈ 0.707)
    v_c = [1.0, 1.0, 0.0, 0.0]
    chunk_c = _make_chunk("partial", v_c, content_hash="cc")

    await backend.write("src1", [chunk_a, chunk_b, chunk_c], mode="append")

    query_vec = _unit_vec(dim, 0)
    hits = await backend.query("src1", query_vec, top_k=3, filters={})

    assert len(hits) == 3
    # Descending order: A (1.0) > C (~0.707) > B (0.0)
    assert hits[0]["text"] == "most similar"
    assert hits[1]["text"] == "partial"
    assert hits[2]["text"] == "orthogonal"
    assert hits[0]["score"] > hits[1]["score"] > hits[2]["score"]


@pytest.mark.asyncio
async def test_query_with_source_path_filter(tmp_path: Path) -> None:
    """Tier 2: SQL filter on source_path restricts results before cosine ranking."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)

    c1 = _make_chunk("from a.py", [1.0, 0.0], content_hash="h1", source_path="a.py")
    c2 = _make_chunk("from b.py", [0.9, 0.1], content_hash="h2", source_path="b.py")

    await backend.write("src1", [c1, c2], mode="append")

    hits = await backend.query(
        "src1", [1.0, 0.0], top_k=5, filters={"source_path": "a.py"}
    )
    assert len(hits) == 1
    assert hits[0]["text"] == "from a.py"


@pytest.mark.asyncio
async def test_query_empty_source_returns_empty(tmp_path: Path) -> None:
    """Tier 2: querying a source whose db does not exist returns an empty list."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)

    hits = await backend.query("nonexistent-source", [1.0, 0.0], top_k=5, filters={})
    assert hits == []


@pytest.mark.asyncio
async def test_drop_removes_db_dir_and_returns_count(tmp_path: Path) -> None:
    """Tier 2: drop removes the source directory and reports chunks_dropped accurately."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)

    chunks = [
        _make_chunk(f"chunk {i}", [float(i), 0.0], content_hash=f"h{i}")
        for i in range(3)
    ]
    await backend.write("src1", chunks, mode="append")

    source_dir = tmp_path / ".reyn" / "index" / "src1"
    assert source_dir.exists()

    result = await backend.drop("src1")
    assert result["removed"] is True
    assert result["chunks_dropped"] == 3
    assert not source_dir.exists()


@pytest.mark.asyncio
async def test_drop_nonexistent_source(tmp_path: Path) -> None:
    """Tier 2: drop on a source that was never indexed returns removed=False, count=0."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    result = await backend.drop("ghost-source")
    assert result["removed"] is False
    assert result["chunks_dropped"] == 0


@pytest.mark.asyncio
async def test_stat_returns_correct_counts_and_last_indexed(tmp_path: Path) -> None:
    """Tier 2: stat reflects chunk count, embedding_model, and last_indexed timestamp."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)

    chunks = [
        _make_chunk("c0", [1.0, 0.0], content_hash="s0", embedding_model="em-v1"),
        _make_chunk("c1", [0.0, 1.0], content_hash="s1", embedding_model="em-v1"),
    ]
    await backend.write("src1", chunks, mode="append")

    stat = await backend.stat("src1")
    assert stat["chunk_count"] == 2
    assert stat["embedding_model"] == "em-v1"
    assert stat["last_indexed"] is not None
    # Basic ISO timestamp sanity check
    assert "T" in stat["last_indexed"]


@pytest.mark.asyncio
async def test_stat_nonexistent_source(tmp_path: Path) -> None:
    """Tier 2: stat on a never-indexed source returns zero/None values."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    stat = await backend.stat("absent")
    assert stat["chunk_count"] == 0
    assert stat["embedding_model"] is None
    assert stat["last_indexed"] is None


@pytest.mark.asyncio
async def test_wal_mode_enabled(tmp_path: Path) -> None:
    """Tier 2: WAL journal_mode is enabled on the underlying SQLite database."""
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    chunk = _make_chunk("wal test", [1.0, 0.0], content_hash="wal1")
    await backend.write("src1", [chunk], mode="append")

    db_file = tmp_path / ".reyn" / "index" / "src1" / "index.db"
    assert db_file.exists()

    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert row[0] == "wal"
    finally:
        conn.close()
