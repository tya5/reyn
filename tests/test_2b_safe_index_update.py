"""Tier 2: reyn.api.safe.index_update — safe-mode ingestion entry point
(FP-0057 Phase 2b, retires reyn.api.safe.embed_index.embed_and_index).

These pin the OS invariants of the new safe-mode entry: a safe-mode python
step's chunks are reconciled (add/update/remove/skip) into a source's SQLite
index via the SAME `index_update` op the LLM-facing tool uses, the
SourceManifest is refreshed, and — critically — a re-run with unchanged
chunks re-embeds NOTHING (DB-as-checkpoint resume = the cost save). Uses a
call-counting real EmbeddingProvider registered via the real
`register_provider` seam (REYN_EMBEDDING_PROVIDER env, set through
`_set_context`) so the zero-re-embed property is asserted on real embed
traffic, not a proxy. No mocks/patches.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reyn.api.safe import index_update as iu
from reyn.data.embedding import register_provider
from reyn.data.embedding.provider import EmbedBatchResult
from reyn.data.index.backends.sqlite import SqliteIndexBackend
from reyn.data.index.source_manifest import get_source_manifest


class CountingFakeProvider:
    """Deterministic provider that records how many texts it has embedded
    (class-level so the count survives `get_provider` re-instantiation)."""

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


def _chunk(text: str, source_path: str) -> dict:
    return {
        "text": text,
        "metadata": {
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "source_path": source_path,
            "source_type": "generic",
        },
    }


@pytest.fixture(autouse=True)
def _wire(tmp_path: Path):
    register_provider("counting_fake", CountingFakeProvider)
    CountingFakeProvider.embedded_texts = 0
    iu._reset_context()
    iu._set_context(workspace_root=tmp_path, provider_name="counting_fake")
    yield
    iu._reset_context()


@pytest.mark.asyncio
async def test_index_update_writes_chunks_and_manifest(tmp_path: Path) -> None:
    """Tier 2: chunks are embedded + written to the source index via the
    shared `index_update` op, and the SourceManifest reflects the result."""
    chunks = [_chunk(f"chunk number {i}", f"doc{i}.md") for i in range(5)]
    result = await iu.index_update_async(
        chunks, source="docs", model="standard",
        description="My docs", path="docs/*.md",
    )
    assert result["added"] == 5
    assert result["chunk_count"] == 5

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
    chunks = [_chunk(f"chunk number {i}", f"doc{i}.md") for i in range(5)]
    await iu.index_update_async(chunks, source="docs", model="standard")
    assert CountingFakeProvider.embedded_texts == 5  # first run embeds all

    CountingFakeProvider.embedded_texts = 0
    result = await iu.index_update_async(
        [_chunk(f"chunk number {i}", f"doc{i}.md") for i in range(5)],
        source="docs", model="standard",
    )
    assert CountingFakeProvider.embedded_texts == 0  # ★zero re-embed
    assert result["added"] == 0
    assert result["updated"] == 0
    assert result["skipped"] == 5


@pytest.mark.asyncio
async def test_resume_embeds_only_new_chunks(tmp_path: Path) -> None:
    """Tier 2: incremental reconcile embeds only the chunks not already indexed."""
    await iu.index_update_async(
        [_chunk(f"chunk number {i}", f"doc{i}.md") for i in range(5)],
        source="docs", model="standard",
    )
    CountingFakeProvider.embedded_texts = 0
    mixed = [_chunk(f"chunk number {i}", f"doc{i}.md") for i in range(5)] + [
        _chunk(f"brand new chunk {j}", f"new{j}.md") for j in range(3)
    ]
    result = await iu.index_update_async(mixed, source="docs", model="standard")
    assert CountingFakeProvider.embedded_texts == 3  # only the new ones
    assert result["added"] == 3
    assert result["skipped"] == 5
    stat = await SqliteIndexBackend(workspace_root=tmp_path).stat("docs")
    assert stat["chunk_count"] == 8


@pytest.mark.asyncio
async def test_changed_content_updates_and_removes_stale(tmp_path: Path) -> None:
    """Tier 2: re-supplying a `source_path` with a changed hash embeds the
    new content and removes the path's stale hash (reconcile, not append-only —
    the retired embed_and_index's `mode="append"` never detected this)."""
    await iu.index_update_async(
        [_chunk("original content", "doc.md")], source="docs", model="standard",
    )
    CountingFakeProvider.embedded_texts = 0
    result = await iu.index_update_async(
        [_chunk("changed content", "doc.md")], source="docs", model="standard",
    )
    assert CountingFakeProvider.embedded_texts == 1
    assert result["updated"] == 1
    stat = await SqliteIndexBackend(workspace_root=tmp_path).stat("docs")
    assert stat["chunk_count"] == 1  # stale hash removed, not accumulated


def test_sync_wrapper(tmp_path: Path) -> None:
    """Tier 2: the sync entry point (what the safe-mode chunker calls) wraps
    the async core via asyncio.run."""
    result = iu.index_update(
        [_chunk("sync path chunk", "s.md")], source="s", model="standard",
    )
    assert result["added"] == 1


@pytest.mark.asyncio
async def test_workspace_defaults_to_cwd(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: with no workspace_root override, the index lands under cwd
    (the cwd==workspace contract the safe-mode chunker relies on)."""
    iu._reset_context()
    iu._set_context(provider_name="counting_fake")  # provider only, ws=cwd
    monkeypatch.chdir(tmp_path)
    await iu.index_update_async([_chunk("cwd chunk", "d.md")], source="d", model="standard")
    assert (tmp_path / ".reyn" / "cache" / "index" / "d" / "index.db").exists()


@pytest.mark.asyncio
async def test_set_context_overrides_cwd(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: an explicit _set_context(workspace_root=...) override wins over
    cwd — the index lands at the override, not the working directory."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    iu._reset_context()
    iu._set_context(workspace_root=other, provider_name="counting_fake")
    monkeypatch.chdir(tmp_path)
    await iu.index_update_async([_chunk("override chunk", "d.md")], source="d", model="standard")
    assert (other / ".reyn" / "cache" / "index" / "d" / "index.db").exists()
    assert not (tmp_path / ".reyn" / "cache" / "index" / "d" / "index.db").exists()


@pytest.mark.asyncio
async def test_sandbox_write_paths_self_gate_denies_outside_path(tmp_path: Path) -> None:
    """Tier 2: #2856 Part B — a phase sandbox write_paths cap that excludes
    this source's index path denies the call. The cap now self-gates at the
    REAL write site (`SqliteIndexBackend.write`, forwarded via the op's
    `default_sandbox_policy`), not a wrapper pre-flight — so unlike the
    retired #2851 pre-flight, the embed call already ran (cost incurred)
    before the write-site denial; `execute_op` catches the `PermissionError`
    and returns a `status="denied"` envelope (it never raises for op-level
    failures — `core/op_runtime/__init__.py`)."""
    other_root = tmp_path / "elsewhere_entirely"
    other_root.mkdir()
    iu._set_context(sandbox_write_paths=[str(other_root)])
    result = await iu.index_update_async(
        [_chunk("should not write", "d.md")], source="d", model="standard",
    )
    assert result["status"] == "denied"
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    stat = await backend.stat("d")
    assert stat["chunk_count"] == 0  # denied before the write landed


@pytest.mark.asyncio
async def test_sandbox_write_paths_self_gate_allows_within_path(tmp_path: Path) -> None:
    """Tier 2: the sandbox self-gate allows a write when the cap covers BOTH
    writes the op performs — the source's own index.db AND the source
    manifest sources.yaml (see test_sandbox_write_paths_manifest_gate_denies_
    outside_config_dir below for the manifest-path-excluded case)."""
    iu._set_context(
        sandbox_write_paths=[str(tmp_path / ".reyn")],
    )
    result = await iu.index_update_async(
        [_chunk("within cap", "d.md")], source="d", model="standard",
    )
    assert result["added"] == 1


@pytest.mark.asyncio
async def test_sandbox_write_paths_manifest_gate_denies_outside_config_dir(
    tmp_path: Path,
) -> None:
    """Tier 2: F3 (#2856 Part B) falsify — `index_update` also upserts the
    source manifest (`.reyn/config/index/sources.yaml`) on every call, not
    just the source's own `.reyn/cache/index/<source>/index.db`. A
    write_paths cap that covers the index cache dir but EXCLUDES
    `.reyn/config/` must still deny the call (manifest write is gated),
    matching the LLM-tool path's own permission gate
    (`core/op_runtime/index_update.py`), which declares file.write authority
    over both paths. The gate now fires at `SourceManifest`'s own real write
    site (`_atomic_write`) — the DB write (embed + backend.write) has
    already succeeded by the time the manifest upsert is attempted and
    denied (real-write-site ordering, not a pre-flight before either write)."""
    iu._set_context(
        sandbox_write_paths=[str(tmp_path / ".reyn" / "cache" / "index")],
    )
    result = await iu.index_update_async(
        [_chunk("manifest write should be denied", "d.md")],
        source="d", model="standard",
    )
    assert result["status"] == "denied"
    assert not (tmp_path / ".reyn" / "config" / "index" / "sources.yaml").exists()
