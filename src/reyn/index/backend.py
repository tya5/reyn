"""IndexBackend protocol and result TypedDicts (ADR-0033 Phase 1)."""
from __future__ import annotations

from typing import Iterable, Literal, Protocol, TypedDict


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
