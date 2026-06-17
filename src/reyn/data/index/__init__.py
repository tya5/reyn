"""Reyn RAG index layer — public API (ADR-0033 Phase 1)."""
from __future__ import annotations

from reyn.data.index.backend import (
    ChunkRecord,
    DropResult,
    IndexBackend,
    StatResult,
    WriteResult,
)
from reyn.data.index.backends.sqlite import SqliteIndexBackend

_BACKENDS: dict[str, type] = {"sqlite": SqliteIndexBackend}


def register_backend(name: str, impl: type) -> None:
    """Register a backend (= phase 2 plugin path)."""
    _BACKENDS[name] = impl


def get_backend(name: str = "sqlite", **kwargs: object) -> IndexBackend:
    """Get backend instance by name. Phase 1 default = sqlite."""
    cls = _BACKENDS[name]
    return cls(**kwargs)  # type: ignore[return-value]


__all__ = [
    "IndexBackend",
    "ChunkRecord",
    "WriteResult",
    "DropResult",
    "StatResult",
    "SqliteIndexBackend",
    "register_backend",
    "get_backend",
]
