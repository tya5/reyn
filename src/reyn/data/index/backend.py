"""IndexBackend protocol and result TypedDicts (ADR-0033 Phase 1)."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal, Protocol, TypedDict


def cache_dir_for_source(workspace_root: Path, source: str) -> Path:
    """Shared cache-dir convention for a logical index source.

    FP-0057 Phase 0: previously ``SqliteIndexBackend`` computed this path
    privately (``backends/sqlite.py::_db_path``) and ``ActionEmbeddingIndex``
    hardcoded a separate ``.reyn/cache/action_index/`` literal in two more
    places (``session.py`` + the ``reyn embeddings`` CLI) — three independent
    copies of "where does a source's index live" with no shared helper. This
    is the single canonical definition: ``<workspace_root>/.reyn/cache/index/
    <source>/``. The action-catalog source rides this same convention as
    ``source="actions"`` post-consolidation (see ``reyn.tools.action_index``).
    """
    return workspace_root / ".reyn" / "cache" / "index" / source


def sources_manifest_path(workspace_root: Path) -> Path:
    """Shared path convention for the source manifest (``sources.yaml``).

    F3 (RAG FP-0057 post-merge sweep): previously this path was hardcoded as
    ``workspace_root / ".reyn" / "config" / "index" / "sources.yaml"`` in two
    independent places — ``SourceManifest.__init__``
    (``data/index/source_manifest.py``) and the ``index_update`` op's own
    permission-check path (``core/op_runtime/index_update.py``) — with a
    third copy about to be added for the safe-mode wrapper's pre-flight
    sandbox gate (``api/safe/index_update.py``). This is the single canonical
    definition, mirroring ``cache_dir_for_source`` above: all three call
    sites route through this helper so the gated path is byte-identical to
    the actual write path by construction.
    """
    return workspace_root / ".reyn" / "config" / "index" / "sources.yaml"


class WriteResult(TypedDict):
    written: int
    skipped: int  # dedup'd via content_hash


class DropResult(TypedDict):
    removed: bool
    chunks_dropped: int


class StatResult(TypedDict):
    chunk_count: int
    embedding_model: str | None  # None if empty source
    last_indexed: str | None  # ISO timestamp or None


class ChunkRecord(TypedDict):
    text: str
    vector: list[float]  # or np.ndarray at runtime
    metadata: dict  # ChunkMetadata as dict
    score: float | None  # query result only, similarity score


class IndexBackend(Protocol):
    # FP-0057 Phase 0: capability flag declaring whether this backend can
    # answer ``existing_hashes`` (the pre-embed dedup / resume key).
    # ``SqliteIndexBackend`` supports it (True). ``IndexBackend`` is an
    # **in-core** pluggability seam — a future alternate in-core backend
    # (e.g. a different local store) may not expose "which content_hashes
    # exist" cheaply and would declare False, so callers can fall back to
    # a full-replace write instead of silently calling a method that
    # can't be answered. Not wired to any caller yet in Phase 0 — this is
    # the seam only.
    existing_hashes_capable: bool

    async def write(
        self,
        source: str,
        chunks: Iterable[ChunkRecord],
        mode: Literal["append", "replace"],
    ) -> WriteResult: ...

    async def query(
        self,
        source: str,
        query_vector: list[float],
        top_k: int,
        filters: dict[str, str],
    ) -> list[ChunkRecord]: ...

    async def drop(self, source: str) -> DropResult: ...

    async def stat(self, source: str) -> StatResult: ...

    async def existing_hashes(self, source: str) -> set[str]:
        """Content hashes already indexed for *source* (pre-embed resume key)."""
        ...

    async def existing_hashes_by_path(self, source: str) -> "dict[str, set[str]]":
        """Content hashes already indexed for *source*, grouped by
        ``source_path`` (FP-0057 Phase 2a — the `index_update` delta-reconcile
        key: which paths are already indexed, and under which hashes, so a
        partial re-ingest can add/update/remove scoped to only the paths it
        re-supplies chunks for)."""
        ...

    async def delete(self, source: str, content_hashes: "Iterable[str]") -> int:
        """Delete rows by `content_hash` for *source* (FP-0057 Phase 2a — the
        `index_update` remove-reconciliation primitive). Returns the count of
        rows actually deleted."""
        ...
