"""Tier 2: recall macro op handler OS invariants (ADR-0033 Phase 1).

Tests use FakeEmbeddingProvider (monkeypatched into the recall handler's
provider-direct embed call, #1303 S-I.4) and a real SqliteIndexBackend for
end-to-end recall dispatch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.embedding.provider import EmbedBatchResult
from reyn.data.index.backend import ChunkRecord
from reyn.data.index.backends.sqlite import SqliteIndexBackend
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import RecallIROp
from reyn.security.permissions.permissions import PermissionDecl

# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------

class FakeEmbeddingProvider:
    """Returns a fixed 3-dim vector [1.0, 0.0, 0.0] for any text."""

    def __init__(self) -> None:
        self._batch_size = 10

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        vectors = [[1.0, 0.0, 0.0] for _ in texts]
        return EmbedBatchResult(vectors=vectors, model=model, total_tokens=len(texts))

    def estimate_tokens(self, texts: list[str]) -> int:
        return len(texts)

    def get_dimension(self, model: str) -> int:
        return 3


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


async def _seed(workspace_root: Path, source: str, chunks: list[ChunkRecord]) -> None:
    backend = SqliteIndexBackend(workspace_root=workspace_root)
    await backend.write(source, chunks, mode="append")


def _chunk(text: str, vec: list[float], ch: str) -> ChunkRecord:
    return ChunkRecord(
        text=text,
        vector=vec,
        metadata={
            "source_path": "f.txt",
            "source_type": "generic",
            "content_hash": ch,
            "embedding_model": "m",
            "chunk_index": 0,
            "size_tokens": len(text.split()),
            "parent_context": None,
        },
        score=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recall_happy_path_returns_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: recall macro embeds query, queries source, returns merged top-K."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.recall as _recall_mod
    monkeypatch.setattr(_recall_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "src1", [
        _chunk("relevant result", [1.0, 0.0, 0.0], "r1"),
        _chunk("less relevant",   [0.0, 1.0, 0.0], "r2"),
    ])

    ctx = _make_ctx(tmp_path)
    op = RecallIROp(
        kind="recall",
        query="test query",
        sources=["src1"],
        top_k=5,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    # With the fake provider returning [1,0,0], "relevant result" has score ≈ 1
    assert len(result["chunks"]) >= 1
    assert result["mode"] in ("semantic", "fallback", "mixed")


@pytest.mark.asyncio
async def test_recall_empty_sources_returns_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: recall with empty sources list returns fallback immediately."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.recall as _recall_mod
    monkeypatch.setattr(_recall_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    op = RecallIROp(
        kind="recall",
        query="anything",
        sources=[],
        top_k=5,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert result["chunks"] == []
    assert result["mode"] == "fallback"


@pytest.mark.asyncio
async def test_recall_merges_multiple_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: recall merges chunks from multiple sources, sorted by score."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.recall as _recall_mod
    monkeypatch.setattr(_recall_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "src1", [_chunk("from source 1", [1.0, 0.0, 0.0], "s1c1")])
    await _seed(tmp_path, "src2", [_chunk("from source 2", [1.0, 0.0, 0.0], "s2c1")])

    ctx = _make_ctx(tmp_path)
    op = RecallIROp(
        kind="recall",
        query="test",
        sources=["src1", "src2"],
        top_k=10,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    texts = [c["text"] for c in result["chunks"]]
    assert "from source 1" in texts
    assert "from source 2" in texts


@pytest.mark.asyncio
async def test_recall_top_k_limits_merged_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: recall top_k limits total chunks returned across all sources."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.recall as _recall_mod
    monkeypatch.setattr(_recall_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    chunks1 = [_chunk(f"a{i}", [1.0, 0.0, 0.0], f"a{i}") for i in range(5)]
    chunks2 = [_chunk(f"b{i}", [1.0, 0.0, 0.0], f"b{i}") for i in range(5)]
    await _seed(tmp_path, "src1", chunks1)
    await _seed(tmp_path, "src2", chunks2)

    ctx = _make_ctx(tmp_path)
    op = RecallIROp(
        kind="recall",
        query="test",
        sources=["src1", "src2"],
        top_k=3,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert not result["chunks"][3:]  # top_k=3 caps result count


@pytest.mark.asyncio
async def test_recall_mode_all_semantic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: when all sources return semantic results, mode='semantic'."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.recall as _recall_mod
    monkeypatch.setattr(_recall_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "s1", [_chunk("chunk", [1.0, 0.0, 0.0], "c1")])

    ctx = _make_ctx(tmp_path)
    op = RecallIROp(
        kind="recall",
        query="anything",
        sources=["s1"],
        top_k=5,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    # s1 has content, embed returns a vector → semantic
    assert result["mode"] == "semantic"


@pytest.mark.asyncio
async def test_recall_embed_failure_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: a provider-direct embed failure → graceful fallback (empty
    chunks, mode=fallback) + a recall_embed_failed event (#1303 S-I.4 replaced
    the embed sub-op error-status check with a try/except around provider.embed)."""
    class _RaisingProvider:
        def __init__(self) -> None:
            self._batch_size = 10

        async def embed(self, texts: list[str], model: str) -> Any:
            raise RuntimeError("embedding endpoint down")

        def estimate_tokens(self, texts: list[str]) -> int:
            return 0

        def get_dimension(self, model: str) -> int:
            return 3

    import reyn.core.op_runtime.recall as _recall_mod
    monkeypatch.setattr(_recall_mod, "get_provider", lambda *a, **kw: _RaisingProvider())
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    op = RecallIROp(
        kind="recall", query="q", sources=["s1"], top_k=5, embedding_model="standard",
    )
    result = await execute_op(op, ctx, caller="control_ir")

    assert result["chunks"] == []
    assert result["mode"] == "fallback"
    assert any(e.type == "recall_embed_failed" for e in ctx.events.all())
