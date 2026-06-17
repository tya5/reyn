"""Tier 2: FP-0043 Component E — concurrency + class-swap detection contract.

Pins ActionEmbeddingIndex behaviour added by the Component E PR:

  1. **Class-swap detection**: rebuilding with a different ``model_class``
     against an identical catalog re-invokes the provider (= vectors
     from the previous model are NOT silently reused).
  2. **Cross-process advisory lock**: when ``persist_dir/.build.lock``
     carries a live PID, a concurrent ``build()`` skips the embed call
     (= no duplicate API cost / duplicate sentence-transformers model
     load). The lock file marks holder PID + timestamp atomically.
  3. **Stale-lock reaping**: a ``.build.lock`` whose recorded PID is
     dead is taken over (= no permanent deadlock after a crash).
  4. **Disk persistence carries model_class**: ``_save_to_disk`` writes
     ``model_class`` into the meta table; ``_try_load_from_disk`` checks
     it. Same catalog hash + same model class → load. Same catalog
     hash + different model class → reject (= rebuild).

No mocks. Real ActionEmbeddingIndex + real ``_FakeProvider`` counting
embed calls + real filesystem operations.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from pathlib import Path
from typing import Any

import pytest

from reyn.data.embedding.provider import EmbedBatchResult
from reyn.tools.action_index import (
    ActionEmbeddingIndex,
    _pid_alive,
    _try_acquire_build_lock,
    compute_catalog_hash,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeProvider:
    """Real-fake EmbeddingProvider; counts embed() invocations + records model."""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.embed_calls: list[str] = []  # model arg per call

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        self.embed_calls.append(model)
        return EmbedBatchResult(
            vectors=[[float((i + 1) * 0.1) for i in range(self.dim)] for _ in texts],
            model=model,
            total_tokens=sum(len(t) // 4 for t in texts),
        )

    def estimate_tokens(self, texts: list[str]) -> int:
        return sum(len(t) // 4 for t in texts)

    def get_dimension(self, model: str) -> int:
        return self.dim


def _items() -> list[dict[str, Any]]:
    return [
        {"qualified_name": "file__read", "short_description": "Read a file"},
        {"qualified_name": "web__search", "short_description": "Search the web"},
    ]


# ── 1. Class-swap detection ──────────────────────────────────────────────────


def test_same_catalog_different_class_triggers_rebuild(tmp_path: Path) -> None:
    """Tier 2: identical items + new model_class → provider.embed called again."""
    provider = _FakeProvider()
    idx = ActionEmbeddingIndex(persist_dir=tmp_path)

    _run(idx.build(_items(), provider, "openai/text-embedding-3-small"))
    assert provider.embed_calls == ["openai/text-embedding-3-small"]

    # Same catalog, different class — must re-embed even though hash matches.
    _run(idx.build(_items(), provider, "sentence-transformers/all-MiniLM-L6-v2"))
    assert provider.embed_calls == [
        "openai/text-embedding-3-small",
        "sentence-transformers/all-MiniLM-L6-v2",
    ]
    # Internal class tracker reflects the latest build.
    assert idx.model_class == "sentence-transformers/all-MiniLM-L6-v2"


def test_same_catalog_same_class_remains_idempotent(tmp_path: Path) -> None:
    """Tier 2: regression guard — class match preserves Phase 2 step 2 idempotency.

    The class-swap check must not break the existing "same hash → no-op"
    contract; a second build() with the same catalog AND the same model
    class must skip the embed call.
    """
    provider = _FakeProvider()
    idx = ActionEmbeddingIndex(persist_dir=tmp_path)

    _run(idx.build(_items(), provider, "standard"))
    _run(idx.build(_items(), provider, "standard"))
    assert provider.embed_calls == ["standard"]  # second call short-circuited


# ── 2. Disk persistence carries model_class ──────────────────────────────────


def test_disk_load_rejects_model_class_mismatch(tmp_path: Path) -> None:
    """Tier 2: cross-process cache reuse blocked when model class differs.

    Mirrors the scenario where two reyn processes share .reyn/action_index/
    but configured against different embedding classes. The first process
    persists vectors tagged with its class; the second loads, sees the
    mismatch, and rebuilds with its own provider rather than serving
    foreign-model vectors.
    """
    provider_a = _FakeProvider()
    idx_a = ActionEmbeddingIndex(persist_dir=tmp_path)
    _run(idx_a.build(_items(), provider_a, "openai/text-embedding-3-small"))
    assert provider_a.embed_calls == ["openai/text-embedding-3-small"]

    # Fresh instance pointing at the same persist_dir but with a different
    # model class — must re-embed because the on-disk meta records the
    # other class.
    provider_b = _FakeProvider()
    idx_b = ActionEmbeddingIndex(persist_dir=tmp_path)
    _run(idx_b.build(_items(), provider_b, "sentence-transformers/all-MiniLM-L6-v2"))
    assert provider_b.embed_calls == ["sentence-transformers/all-MiniLM-L6-v2"]


def test_disk_load_accepts_matching_class_and_catalog(tmp_path: Path) -> None:
    """Tier 2: process restart with same class + catalog → disk-only path.

    The second instance does NOT invoke its provider; the vectors are
    loaded from SQLite.
    """
    provider_a = _FakeProvider()
    idx_a = ActionEmbeddingIndex(persist_dir=tmp_path)
    _run(idx_a.build(_items(), provider_a, "standard"))

    provider_b = _FakeProvider()
    idx_b = ActionEmbeddingIndex(persist_dir=tmp_path)
    _run(idx_b.build(_items(), provider_b, "standard"))
    assert provider_b.embed_calls == []  # loaded from disk; embed not called
    assert idx_b.is_ready()
    assert idx_b.model_class == "standard"


# ── 3. Cross-process advisory lock ──────────────────────────────────────────


def test_concurrent_build_skips_when_lock_held_by_live_pid(tmp_path: Path) -> None:
    """Tier 2: another live process holding .build.lock → embed call skipped.

    Simulates the chainlit + reyn-web parallel-session race tui-coder
    reported: both decide to rebuild simultaneously. Without the file
    lock both would call provider.embed() and one would lose the disk
    write race. With the lock, only the holder rebuilds; the other
    falls back to whatever's currently on disk (= here, nothing → empty
    state, which is fine; the holder's eventual save will be picked up
    on the next build() call).
    """
    # Stage: write a lock file claiming the current (live) PID is mid-build.
    lock_path = tmp_path / ".build.lock"
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "ts": time.time()}),
        encoding="utf-8",
    )

    provider = _FakeProvider()
    idx = ActionEmbeddingIndex(persist_dir=tmp_path)
    _run(idx.build(_items(), provider, "standard"))

    # The lock was held → embed must NOT have been called.
    assert provider.embed_calls == []
    # The index also did not mutate its in-memory state (= consistent
    # with the "another process owns this build" semantics).
    assert not idx.is_ready()
    # Clean up the staged lock so other tests don't inherit it.
    lock_path.unlink(missing_ok=True)


def test_stale_lock_is_reaped(tmp_path: Path) -> None:
    """Tier 2: a .build.lock whose PID is dead is taken over (no deadlock)."""
    lock_path = tmp_path / ".build.lock"
    tmp_path.mkdir(parents=True, exist_ok=True)
    # Pick a PID that's almost certainly dead (= a very large number
    # well outside the typical PID range). os.kill(pid, 0) will surface
    # ProcessLookupError for it.
    dead_pid = 2**31 - 1  # max int32; not allocated by any kernel
    assert not _pid_alive(dead_pid), (
        f"precondition: PID {dead_pid} must not be alive for this test"
    )
    lock_path.write_text(
        json.dumps({"pid": dead_pid, "ts": time.time()}),
        encoding="utf-8",
    )

    provider = _FakeProvider()
    idx = ActionEmbeddingIndex(persist_dir=tmp_path)
    _run(idx.build(_items(), provider, "standard"))

    # Stale lock reaped → embed called normally; build completed.
    assert provider.embed_calls == ["standard"]
    assert idx.is_ready()
    # Lock file removed on context exit.
    assert not lock_path.exists()


def test_corrupt_lock_file_is_treated_as_stale(tmp_path: Path) -> None:
    """Tier 2: malformed .build.lock (= partial write, garbage) is recoverable.

    A crashed previous process may leave a half-written lock; the next
    builder should treat it as stale rather than wedging forever.
    """
    lock_path = tmp_path / ".build.lock"
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not json at all }}}", encoding="utf-8")

    provider = _FakeProvider()
    idx = ActionEmbeddingIndex(persist_dir=tmp_path)
    _run(idx.build(_items(), provider, "standard"))

    assert provider.embed_calls == ["standard"]


# ── 4. Lock helper unit invariants ──────────────────────────────────────────


def test_try_acquire_lock_yields_true_then_releases(tmp_path: Path) -> None:
    """Tier 2: helper acquires the lock and removes the marker on exit."""
    with _try_acquire_build_lock(tmp_path) as got:
        assert got is True
        assert (tmp_path / ".build.lock").exists()
    assert not (tmp_path / ".build.lock").exists()


def test_try_acquire_lock_yields_false_when_holder_alive(tmp_path: Path) -> None:
    """Tier 2: helper yields False when a live PID holds the lock."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".build.lock").write_text(
        json.dumps({"pid": os.getpid(), "ts": time.time()}),
        encoding="utf-8",
    )
    with _try_acquire_build_lock(tmp_path) as got:
        assert got is False
    # Holder lock is preserved (= we did not unlink someone else's marker).
    assert (tmp_path / ".build.lock").exists()
    (tmp_path / ".build.lock").unlink()


def test_pid_alive_dead_pid_returns_false() -> None:
    """Tier 2: _pid_alive sanity — a huge PID returns False (= no false-positive)."""
    assert _pid_alive(2**31 - 1) is False


def test_pid_alive_self_returns_true() -> None:
    """Tier 2: _pid_alive of os.getpid() returns True."""
    assert _pid_alive(os.getpid()) is True


# ── 5. Empty catalog records model_class too ────────────────────────────────


def test_empty_catalog_records_class_for_subsequent_match(tmp_path: Path) -> None:
    """Tier 2: building over an empty catalog still imprints model_class.

    Otherwise the next call with the same empty catalog + same class
    would re-enter the build path (= regression against the Phase 2
    step 2 empty-catalog short-circuit).
    """
    provider = _FakeProvider()
    idx = ActionEmbeddingIndex(persist_dir=tmp_path)
    _run(idx.build([], provider, "standard"))
    assert idx.model_class == "standard"
    # Same args → no-op (no second embed call).
    _run(idx.build([], provider, "standard"))
    assert provider.embed_calls == []  # empty catalog never calls embed
