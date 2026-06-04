"""Tier 2: reyn.safe.embed_index provider-direct embed+index (#1303 Stage I).

The safe-mode API folds the old embed + index_write run-ops into one streaming
call. These pin the OS invariants: chunks are embedded + written to the SQLite
index, the SourceManifest is refreshed, and — critically — a re-run skips
already-indexed content_hashes BEFORE embedding (DB-as-checkpoint resume =
0 re-embed, the cost save). Uses a call-counting fake provider so the
zero-re-embed property is asserted on real embed traffic, not a proxy.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.embedding import register_provider
from reyn.embedding.provider import EmbedBatchResult
from reyn.index import SqliteIndexBackend
from reyn.index.source_manifest import get_source_manifest
from reyn.safe import embed_index as ei


class CountingFakeProvider:
    """Deterministic provider that records how many texts it has embedded
    (class-level so the count survives ``get_provider`` re-instantiation)."""

    embedded_texts: int = 0

    def __init__(self, config: dict | None = None) -> None:
        self._batch_size = 100

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        CountingFakeProvider.embedded_texts += len(texts)
        return EmbedBatchResult(
            vectors=[[float(len(t)), 0.0, 0.0, 0.0] for t in texts],
            model=model or "fake-embed-model",
            total_tokens=sum(len(t) for t in texts),
        )

    def estimate_tokens(self, texts: list[str]) -> int:
        return sum(len(t) for t in texts)

    def get_dimension(self, model: str) -> int:
        return 4


def _chunk(text: str, idx: int) -> dict:
    import hashlib

    return {
        "text": text,
        "metadata": {
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "source_path": "doc.md",
            "source_type": "generic",
            "chunk_index": idx,
            "size_tokens": len(text),
        },
    }


@pytest.fixture(autouse=True)
def _wire(tmp_path: Path):
    register_provider("counting_fake", CountingFakeProvider)
    CountingFakeProvider.embedded_texts = 0
    ei._reset_context()
    ei._set_context(workspace_root=tmp_path, provider_name="counting_fake")
    yield
    ei._reset_context()


@pytest.mark.asyncio
async def test_embed_and_index_writes_chunks_and_manifest(tmp_path: Path) -> None:
    """Tier 2: chunks are embedded + written to the source index, and the
    SourceManifest entry reflects the chunk count + model."""
    chunks = [_chunk(f"chunk number {i}", i) for i in range(5)]
    result = await ei.embed_and_index_async(
        chunks, source="docs", model="standard",
        description="My docs", path="docs/*.md",
    )
    assert result["embedded"] == 5
    assert result["written"] == 5
    assert result["skipped_embed"] == 0

    backend = SqliteIndexBackend(workspace_root=tmp_path)
    stat = await backend.stat("docs")
    assert stat["chunk_count"] == 5

    entry = await get_source_manifest(tmp_path).get("docs")
    assert entry is not None
    assert entry.chunk_count == 5
    assert entry.description == "My docs"
    assert entry.path == "docs/*.md"


@pytest.mark.asyncio
async def test_resume_skips_reembed(tmp_path: Path) -> None:
    """Tier 2: ★core — a re-run with the same chunks re-embeds NOTHING; the
    DB content_hashes are skipped before embedding (cost-save resume)."""
    chunks = [_chunk(f"chunk number {i}", i) for i in range(5)]
    await ei.embed_and_index_async(chunks, source="docs", model="standard")
    assert CountingFakeProvider.embedded_texts == 5  # first run embeds all

    CountingFakeProvider.embedded_texts = 0
    result = await ei.embed_and_index_async(
        [_chunk(f"chunk number {i}", i) for i in range(5)],
        source="docs", model="standard",
    )
    assert CountingFakeProvider.embedded_texts == 0  # ★zero re-embed
    assert result["embedded"] == 0
    assert result["skipped_embed"] == 5


@pytest.mark.asyncio
async def test_resume_embeds_only_new_chunks(tmp_path: Path) -> None:
    """Tier 2: incremental append embeds only the chunks not already indexed."""
    await ei.embed_and_index_async(
        [_chunk(f"chunk number {i}", i) for i in range(5)],
        source="docs", model="standard",
    )
    CountingFakeProvider.embedded_texts = 0
    # 5 old + 3 new
    mixed = [_chunk(f"chunk number {i}", i) for i in range(5)] + [
        _chunk(f"brand new chunk {j}", 100 + j) for j in range(3)
    ]
    result = await ei.embed_and_index_async(mixed, source="docs", model="standard")
    assert CountingFakeProvider.embedded_texts == 3  # only the new ones
    assert result["embedded"] == 3
    assert result["skipped_embed"] == 5
    stat = await SqliteIndexBackend(workspace_root=tmp_path).stat("docs")
    assert stat["chunk_count"] == 8


@pytest.mark.asyncio
async def test_replace_mode_rebuilds(tmp_path: Path) -> None:
    """Tier 2: replace mode clears the prior index then re-embeds from scratch."""
    await ei.embed_and_index_async(
        [_chunk(f"old chunk {i}", i) for i in range(4)],
        source="docs", model="standard",
    )
    CountingFakeProvider.embedded_texts = 0
    result = await ei.embed_and_index_async(
        [_chunk(f"fresh chunk {i}", i) for i in range(2)],
        source="docs", model="standard", mode="replace",
    )
    assert CountingFakeProvider.embedded_texts == 2  # replace re-embeds all
    assert result["embedded"] == 2
    stat = await SqliteIndexBackend(workspace_root=tmp_path).stat("docs")
    assert stat["chunk_count"] == 2  # old 4 gone


@pytest.mark.asyncio
async def test_streaming_batches(tmp_path: Path) -> None:
    """Tier 2: more chunks than the batch size stream through multiple flushes
    and all land in the index."""
    chunks = (_chunk(f"streamed chunk {i}", i) for i in range(25))  # generator
    result = await ei.embed_and_index_async(
        chunks, source="big", model="standard", batch_size=10,
    )
    assert result["embedded"] == 25
    stat = await SqliteIndexBackend(workspace_root=tmp_path).stat("big")
    assert stat["chunk_count"] == 25


def test_sync_wrapper(tmp_path: Path) -> None:
    """Tier 2: the sync entry point (what the safe-mode chunker calls) wraps
    the async core via asyncio.run."""
    result = ei.embed_and_index(
        [_chunk("sync path chunk", 0)], source="s", model="standard",
    )
    assert result["embedded"] == 1


@pytest.mark.asyncio
async def test_workspace_defaults_to_cwd(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: with no workspace_root override, the index lands under cwd
    (the cwd==workspace contract the safe-mode chunker relies on)."""
    ei._reset_context()
    ei._set_context(provider_name="counting_fake")  # provider only, ws=cwd
    monkeypatch.chdir(tmp_path)
    await ei.embed_and_index_async([_chunk("cwd chunk", 0)], source="d", model="standard")
    assert (tmp_path / ".reyn" / "index" / "d" / "index.db").exists()


@pytest.mark.asyncio
async def test_set_context_overrides_cwd(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: an explicit _set_context(workspace_root=...) override wins over
    cwd — the index lands at the override, not the working directory."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    ei._reset_context()
    ei._set_context(workspace_root=other, provider_name="counting_fake")
    monkeypatch.chdir(tmp_path)
    await ei.embed_and_index_async([_chunk("override chunk", 0)], source="d", model="standard")
    assert (other / ".reyn" / "index" / "d" / "index.db").exists()
    assert not (tmp_path / ".reyn" / "index" / "d" / "index.db").exists()
