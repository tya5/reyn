"""FP-0066 P3a (#3247) — skill + memory knowledge ingest (sync-in-op) + G3
delete-de-index + loud-kind + the IndexCoordinator's first real-producer
recovery e2e.

Covers (per the architect's P3 firm §7 tracked items + P3a scope):
  1. **loud-kind**: ``register_builder`` now requires ``kind`` (no silent
     "backfill" default) — a call omitting it is a ``TypeError`` at the
     call site. The pre-taxonomy ``sources.yaml`` on-disk coercion (an
     entry with no/garbled ``kind`` field) is UNCHANGED and still reads
     back as ``"backfill"`` (a genuine migration default, not touched by
     this PR).
  2. **real-producer dirty→heal recovery e2e**: a real ``remember`` (via
     ``reyn.tools.memory._handle_remember``, the fallback/non-router path
     — the SAME handler ``RouterLoop._remember`` wires to production) with
     the embedding provider failing → the op still SUCCEEDS (§G2
     best-effort) + the "knowledge_memory" source is left ``dirty`` → a
     subsequent ``search_await`` with a WORKING provider HEALS it (re-
     ingests, state returns ``clean``). This is the IndexCoordinator's
     (#3259 P2a) dirty/heal recovery path exercised through a REAL
     production-shaped producer for the first time (P2a/P2b/P2d's own
     tests only exercised it via synthetic ``register_builder`` calls).
  3. **G3 sync de-index**: ``forget_memory`` (fallback path) removes the
     just-forgotten entry's embedded row SYNCHRONOUSLY — a direct backend
     query after ``forget_memory`` confirms the content_hash is gone.
  4. **§G2 best-effort**: a provider failure during ``remember`` does not
     raise / does not fail the tool call — the write + listing-index regen
     still succeed and the handler still returns ``{"saved": ...}``.

No mocks — real ``SqliteIndexBackend``, real ``SourceManifest``, real
``Workspace``/``OpContext``/``ToolContext``; a plain ``FakeEmbeddingProvider``
(same established convention as ``tests/test_index_coordinator_3247_p2a.py``)
stands in for the litellm boundary via the ``get_provider`` monkeypatch seam.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.data.index import SqliteIndexBackend
from reyn.data.index.coordinator import IndexCoordinator, get_index_coordinator
from reyn.data.index.knowledge_ingest import (
    KNOWLEDGE_MEMORY_SOURCE_ID,
    memory_content_hash,
)
from reyn.data.index.source_manifest import get_source_manifest
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools import memory as memory_tools
from reyn.tools.types import ToolContext


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeEmbeddingProvider:
    """Deterministic canned vectors, one per input text — no litellm call.
    Mirrors ``tests/test_index_coordinator_3247_p2a.py::_FakeEmbeddingProvider``."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.calls.append(tuple(texts))
        if self.fail:
            raise RuntimeError("provider unreachable (simulated)")
        vectors = [[float((hash((t, i)) % 1000) / 1000.0) for i in range(4)] for t in texts]
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


def _make_ctx(tmp_path: Path) -> ToolContext:
    events = EventLog()
    # memory.py's fallback path writes an ABSOLUTE state_dir path (Workspace's
    # own _resolve_write requires an explicit permission grant for absolute
    # writes, unrelated to this PR) — a real PermissionResolver anchored at
    # tmp_path grants its default write zone, matching state_dir=tmp_path/.reyn.
    perm = PermissionResolver({}, project_root=tmp_path)
    ws = Workspace(base_dir=tmp_path, events=events, permission_resolver=perm)
    return ToolContext(
        events=events, permission_resolver=perm, workspace=ws, caller_kind="router",
        router_state=None,
    )


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)

    def _enabled() -> bool:
        return True
    monkeypatch.setattr(_embed_mod, "_is_embedding_enabled", _enabled)


# ── 1. loud-kind ─────────────────────────────────────────────────────────


def test_register_builder_requires_kind(tmp_path: Path) -> None:
    """Tier 2: register_builder omitting `kind` is a TypeError (no silent
    "backfill" default for a NEW registration) — #3247 firm §7(b)."""
    coord = IndexCoordinator(tmp_path)

    async def _build_fn():
        raise AssertionError("not reached")

    with pytest.raises(TypeError):
        coord.register_builder("x", _build_fn)  # type: ignore[call-arg]


def test_pretaxonomy_sources_yaml_entry_still_coerces_to_backfill() -> None:
    """Tier 1: a persisted sources.yaml entry predating the kind taxonomy
    (no `kind` key) still reads back as "backfill" — the migration default
    this PR explicitly preserves, distinct from the register_builder
    loud-kind change (#3247 firm P3a scope item 1)."""
    from reyn.data.index.source_manifest import SourceEntry

    entry = SourceEntry.from_dict("legacy_source", {"description": "d", "path": "p"})
    assert entry.kind == "backfill"


# ── 2/4. real-producer dirty->heal recovery e2e + §G2 best-effort ────────


def test_remember_survives_provider_failure_and_heals_on_next_search_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: a real `remember` (reyn.tools.memory._handle_remember, the
    same handler RouterLoop._remember production-wires to) with the
    embedding provider FAILING still succeeds (§G2 best-effort) and leaves
    "knowledge_memory" dirty; a later search_await with a WORKING provider
    heals it (re-ingests, state -> clean) — the IndexCoordinator's (#3259
    P2a) dirty/heal recovery path exercised through a real producer op."""
    ctx = _make_ctx(tmp_path)
    failing_provider = _FakeEmbeddingProvider(fail=True)
    _patch_provider(monkeypatch, failing_provider)

    result = _run(memory_tools._handle_remember(
        {"slug": "note1", "name": "Note", "description": "d", "type": "user", "body": "hello"},
        ctx, layer="shared",
    ))

    assert "error" not in result, "remember must succeed despite the provider failure (§G2)"
    assert result["saved"] == "note1"

    manifest = get_source_manifest(tmp_path)
    entry = _run(manifest.get(KNOWLEDGE_MEMORY_SOURCE_ID))
    assert entry is not None
    assert entry.state in ("dirty", "error"), "a provider failure must leave the source needing a heal"

    # Heal: a working provider + search_await re-ingests.
    working_provider = _FakeEmbeddingProvider(fail=False)
    _patch_provider(monkeypatch, working_provider)
    coordinator = get_index_coordinator(tmp_path)
    _run(coordinator.search_await(KNOWLEDGE_MEMORY_SOURCE_ID))

    healed = _run(manifest.get(KNOWLEDGE_MEMORY_SOURCE_ID))
    assert healed is not None
    assert healed.state == "clean", "search_await must heal the dirty source once the provider works"
    assert healed.chunk_count >= 1


# ── 3. G3 sync de-index ───────────────────────────────────────────────────


def test_forget_memory_sync_deindexes_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: forget_memory (reyn.tools.memory._handle_forget_memory, the
    fallback path RouterLoop._forget production-wires to as well) removes
    the entry's embedded row SYNCHRONOUSLY — a direct backend query after
    forget confirms the content_hash is gone (§G3, not best-effort)."""
    ctx = _make_ctx(tmp_path)
    provider = _FakeEmbeddingProvider(fail=False)
    _patch_provider(monkeypatch, provider)

    _run(memory_tools._handle_remember(
        {"slug": "note2", "name": "Note2", "description": "d", "type": "user", "body": "body2"},
        ctx, layer="shared",
    ))
    manifest = get_source_manifest(tmp_path)
    before = _run(manifest.get(KNOWLEDGE_MEMORY_SOURCE_ID))
    assert before is not None and before.state == "clean" and before.chunk_count == 1

    backend = SqliteIndexBackend(workspace_root=tmp_path)
    existing_hashes = _run(backend.existing_hashes(KNOWLEDGE_MEMORY_SOURCE_ID))
    assert memory_content_hash("shared", "note2") in existing_hashes

    forget_result = _run(memory_tools._handle_forget_memory(
        {"layer": "shared", "slug": "note2"}, ctx,
    ))
    assert forget_result.get("deleted") == "note2"

    remaining_hashes = _run(backend.existing_hashes(KNOWLEDGE_MEMORY_SOURCE_ID))
    assert memory_content_hash("shared", "note2") not in remaining_hashes, (
        "a stale embedded row for a forgotten entry must not survive forget_memory"
    )


def test_delete_entries_raises_on_lock_contention_not_silent_skip(tmp_path: Path) -> None:
    """Tier 2: IndexCoordinator.delete_entries RAISES RuntimeError (never
    silently returns 0 / silently succeeds) when the source's build_lock is
    held by another in-flight build.

    Pins the §G3 "sync, not best-effort" guarantee's raise-not-skip shape:
    a silent skip here would leave a stale, still-searchable row behind for
    content the caller believes was de-indexed — exactly the failure §G3
    exists to prevent. If a future edit regresses ``delete_entries`` from
    ``raise RuntimeError(...)`` to a silent ``return 0``, this test must go
    RED (verified manually: flipping the raise to ``return 0`` fails this
    test; restoring it passes — see PR discussion on #3247/#3263).

    Real instances only: a real ``IndexCoordinator`` + the real
    ``try_acquire_build_lock`` primitive (same one ``delete_entries`` itself
    uses) held open in-process to simulate contention — PID-liveness checks
    against the CURRENT process's own PID are always "alive", so holding
    the lock open in this test process is a faithful simulation of another
    process's in-flight build, no mock required.
    """
    from reyn.data.index.backend import cache_dir_for_source
    from reyn.data.index.build_lock import try_acquire_build_lock

    coord = IndexCoordinator(tmp_path)
    lock_dir = cache_dir_for_source(tmp_path, KNOWLEDGE_MEMORY_SOURCE_ID)

    with try_acquire_build_lock(lock_dir) as got_lock:
        assert got_lock is True, "test setup: must actually hold the lock to simulate contention"
        with pytest.raises(RuntimeError):
            _run(coord.delete_entries(KNOWLEDGE_MEMORY_SOURCE_ID, ["some_content_hash"]))
