"""FP-0066 P2b (#3247) / P2-convergence PR1 (#3270 §2) — per-kind source
split + action-catalog migration through the SINGLE ``ensure_built``.

P2-convergence PR1 eliminated ``IndexCoordinator.ensure_built_self_contained``
(the two-path Coordinator API #3247 firm's P2b originally introduced for the
action-catalog, since ``ActionEmbeddingIndex.build()`` used to own its own
cross-process lock + write). ``ActionEmbeddingIndex`` lost both its locks
(this module) and its material-generation policy moved into
``prepare_material`` (a ``BuildFn`` — see ``reyn.tools.action_index``); the
Coordinator's single ``ensure_built``/``register_builder`` is now the ONE
entry point for every registered source, action-catalog included. This file
is the P2-convergence-PR1 rewrite of the P2b equivalence suite — the fake
below now implements the ``prepare_material``/``adopt_build_result`` shape
instead of the retired self-contained ``build()``-does-everything shape, and
routes actual writes through the REAL ``embed_verify_write`` (via a real
``OpContext`` + a fake embedding provider — same convention as
``tests/test_index_coordinator_3247_p2a.py``) so the Coordinator's own
lock/write machinery is genuinely exercised, not bypassed.

Covers:
  1. ``SourceEntry.kind`` taxonomy — persist + coerce-default ("backfill").
  2. **Equivalence test (mandatory)**: ``RouterLoop._ensure_action_index_
     built`` (now routed through ``ensure_built``) reproduces the
     pre-PR1 observable behavior for the 5 pinned cases — eager→sync
     build, non-eager→background, disk-adopt hit→no rebuild, build
     failure→memoized (not re-attempted), ready-gate reflects state.
  3. **Mandatory strip-falsify co-vet gates** (#3270 §5): self-deadlock/
     silent-no-op non-recurrence, embed-cost duplicate-avoidance,
     register_builder production fail-closed completeness.

No mocks — real ``IndexCoordinator``, real ``SourceManifest``, real
``RouterLoop`` instances (constructed the same minimal-subclass way
``tests/test_action_embedding_build_failure_1458.py`` already does, since
a full host/session is not needed to exercise this orchestration layer);
a plain fake index (real class, not a Mock) stands in for
``ActionEmbeddingIndex`` so build success/failure/timing is controllable
without a real embedding provider network call; a plain fake embedding
provider (monkeypatched into the real `embed` op — established convention)
stands in for the litellm boundary.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.index.backend import ChunkRecord, cache_dir_for_source
from reyn.data.index.build_lock import try_acquire_build_lock
from reyn.data.index.coordinator import BuildMaterial, IndexCoordinator
from reyn.data.index.source_manifest import SourceEntry, SourceManifest
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.permissions import PermissionDecl


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeEmbeddingProvider:
    """Deterministic canned vectors, one per input text (no litellm call)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.calls.append(tuple(texts))
        vectors = [[float((hash((t, i)) % 1000) / 1000.0) for i in range(4)] for t in texts]
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


def _op_ctx_for(provider: Any, monkeypatch: pytest.MonkeyPatch) -> OpContext:
    """Real OpContext whose `embed` op resolves to ``provider`` (mirrors
    ``tests/test_index_coordinator_3247_p2a.py::_ctx_for``)."""
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(workspace=ws, events=events, permission_decl=PermissionDecl())


def _to_chunk_record(item: dict, vector: list[float], resolved_model: str) -> ChunkRecord:
    return ChunkRecord(
        text=item["text"],
        vector=list(vector),
        metadata={
            "source_path": item["id"],
            "source_type": "action",
            "content_hash": item["id"],
            "embedding_model": resolved_model,
            "chunk_index": 0,
            "size_tokens": 0,
            "parent_context": None,
            "extra": {},
        },
    )


# ── 1. SourceEntry.kind taxonomy ──────────────────────────────────────────


def test_kind_persists_and_round_trips(tmp_path: Path) -> None:
    """Tier 2: kind survives a to_dict/from_dict + on-disk round trip."""
    manifest = SourceManifest(tmp_path)
    entry = SourceEntry(
        name="actions", description="action catalog", path="", kind="static",
    )
    _run(manifest.upsert(entry))

    fresh = SourceManifest(tmp_path)
    reloaded = _run(fresh.get("actions"))
    assert reloaded is not None
    assert reloaded.kind == "static"


def test_kind_defaults_to_backfill_when_missing_or_garbled(tmp_path: Path) -> None:
    """Tier 2: a pre-P2b (or operator-edited garbled) sources.yaml entry
    coerces to "backfill" — the documented safe default (predates the
    taxonomy), mirroring the existing ``state`` coercion rationale."""
    assert SourceEntry.from_dict("x", {}).kind == "backfill"
    assert SourceEntry.from_dict("x", {"kind": "not-a-real-kind"}).kind == "backfill"
    assert SourceEntry.from_dict("x", {"kind": "dynamic"}).kind == "dynamic"
    assert SourceEntry.from_dict("x", {"kind": "static"}).kind == "static"


def test_freshly_created_entry_uses_registered_kind(tmp_path: Path) -> None:
    """Tier 2: register_builder + ensure_built on a never-seen source_id
    tags the NEW entry with the registered kind, not the "backfill"
    dataclass default. Reads back through a SEPARATE ``SourceManifest``
    instance against the same file SSoT (public surface only — no
    private-state introspection)."""
    manifest = SourceManifest(tmp_path)
    coord = IndexCoordinator(tmp_path, manifest=manifest)

    async def _build() -> BuildMaterial | None:
        return None

    coord.register_builder("actions", _build, kind="static")
    _run(coord.ensure_built("actions", await_completion=True))
    entry = _run(manifest.get("actions"))
    assert entry is not None
    assert entry.kind == "static"


# ── 2. The action-catalog fake + the 5-case equivalence set ──────────────


class _FakeActionIndex:
    """Real fake standing in for ``ActionEmbeddingIndex`` — controllable
    success/failure/timing, no real embedding network call. Implements the
    P2-convergence PR1 shape: ``prepare_material`` (lock-free, write-free
    material generation) + ``adopt_build_result`` (state sync after the
    CALLER's write succeeds) instead of the retired self-sufficient
    ``build()``-does-everything shape.

    A REAL ``BuildMaterial`` is returned (when not failing/already-ready)
    so the Coordinator's REAL ``embed_verify_write`` genuinely runs against
    it — ``ctx``/``model_class`` are threaded straight through from the
    caller, exactly as ``ActionEmbeddingIndex.prepare_material`` does.
    """

    def __init__(self, *, should_fail: bool = False, delay: float = 0.0) -> None:
        self.should_fail = should_fail
        self.delay = delay
        self.prepare_calls = 0
        self._ready = False
        self._size = 0
        self.source_name = "actions"

    def is_ready(self) -> bool:
        return self._ready

    def size(self) -> int:
        return self._size

    async def prepare_material(
        self, items: list[dict], ctx: Any, model_class: str,
    ) -> BuildMaterial | None:
        self.prepare_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.should_fail:
            raise RuntimeError("simulated provider failure")
        if self._ready:
            return None  # already built — mirrors in-memory idempotency
        return BuildMaterial(
            items=items,
            texts=[it["text"] for it in items],
            to_chunk_record=_to_chunk_record,
            model_class=model_class,
            ctx=ctx,
        )

    def adopt_build_result(self, catalog_hash: str, model_class: str, chunk_count: int) -> None:
        self._ready = True
        self._size = chunk_count


class _StubHost:
    """Minimal host — only what RouterLoop._ensure_action_index_built /
    _fetch_action_catalog_items touch."""

    def __init__(self, op_ctx: Any) -> None:
        self.events = _StubEvents()
        self.op_ctx_stub = op_ctx

    def make_router_op_context(self) -> Any:
        return self.op_ctx_stub


class _StubEvents:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, kind: str, **kwargs: Any) -> None:
        self.emitted.append({"kind": kind, **kwargs})


class _LoopForP2b(RouterLoop):
    """RouterLoop subclass exercising the P2b/P2-convergence-PR1
    orchestration methods without a full host/session — same minimal-
    subclass pattern as ``test_action_embedding_build_failure_1458.py``."""

    def __init__(self, workspace_root: Path, op_ctx: Any) -> None:
        self.host = _StubHost(op_ctx)  # type: ignore[assignment]
        self.chain_id = "test-chain"
        self._workspace_root_for_test = workspace_root

    async def _build_router_caller_state(self) -> Any:
        return None

    async def _fetch_action_catalog_items(self) -> list[dict]:
        # Bypasses the real list_actions/ToolContext plumbing (irrelevant
        # to this orchestration-layer test) — returns a fixed 2-item
        # catalog, mirroring _FakeActionIndex's expectations.
        return [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]

    def _get_index_coordinator(self) -> IndexCoordinator:
        # Deterministic per-test coordinator instance (bypasses the
        # module singleton so tests don't leak state across each other).
        if not hasattr(self, "_test_coordinator"):
            self._test_coordinator = IndexCoordinator(self._workspace_root_for_test)
        return self._test_coordinator


def test_eager_awaits_sync_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: equivalence case 1 — eager (await_completion=True) runs the
    build INLINE — by the time the call returns, the index is ready."""
    ctx = _op_ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    loop = _LoopForP2b(tmp_path, ctx)
    idx = _FakeActionIndex()
    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    assert idx.is_ready() is True
    assert idx.size() == 2
    assert idx.prepare_calls == 1
    coordinator = loop._get_index_coordinator()
    assert _run(coordinator.is_ready("actions")) is True


def test_non_eager_schedules_background(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: equivalence case 2 — non-eager (await_completion=False)
    schedules the build in the BACKGROUND — the call returns before the
    (delayed) build completes, and the index becomes ready only once the
    background task finishes."""
    ctx = _op_ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    loop = _LoopForP2b(tmp_path, ctx)
    idx = _FakeActionIndex(delay=0.05)

    async def _scenario() -> None:
        await loop._ensure_action_index_built(
            idx, "provider", "standard", await_completion=False,
        )
        # Returned immediately — build not done yet (background, not sync).
        assert idx.is_ready() is False
        coordinator = loop._get_index_coordinator()
        # Poll the PUBLIC readiness gate for the scheduled background task
        # to complete (bounded — no private-state introspection, mirrors
        # test_index_coordinator_3247_p2a.py's equivalent poll).
        for _ in range(200):
            if idx.is_ready():
                break
            await asyncio.sleep(0.01)
        assert idx.is_ready() is True
        assert await coordinator.is_ready("actions") is True

    _run(_scenario())


def test_disk_adopt_hit_skips_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: equivalence case 3 — once the index itself reports ready
    (the analogue of a disk-adopt cache hit — a fresh process/instance
    that finds a completed prior build), a second
    ``_ensure_action_index_built`` call is a cheap no-op (does not
    re-invoke ``prepare_material`` at all)."""
    ctx = _op_ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    loop = _LoopForP2b(tmp_path, ctx)
    idx = _FakeActionIndex()
    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    assert idx.prepare_calls == 1

    # Second call — index is still ready (nothing invalidated it) — must
    # be a no-op (no re-embed cost). The wrapper's OWN is_ready() gate
    # short-circuits before even touching the Coordinator.
    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    assert idx.prepare_calls == 1, "a ready source must not rebuild"


def test_build_failure_memoized_not_reattempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: equivalence case 4 — a build failure is memoized SOLELY on
    the Coordinator's own failure-memo (``build_failed``, P2-convergence
    PR2 (#3270 §3) — the twin RouterLoop-side flag is retired) — and the
    production retry guard (checked by the caller before invoking again)
    prevents a second attempt."""
    ctx = _op_ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    loop = _LoopForP2b(tmp_path, ctx)
    idx = _FakeActionIndex(should_fail=True)

    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    assert idx.prepare_calls == 1
    coordinator = loop._get_index_coordinator()
    assert coordinator.build_failed("actions") is True
    assert not hasattr(loop, "_action_index_build_failed"), (
        "the twin RouterLoop-side flag must no longer exist (P2-convergence PR2)"
    )

    # Production retry guard (mirrors RouterLoop.run()'s own gate): do NOT
    # call again once the Coordinator's failure-memo is set.
    if not coordinator.build_failed("actions"):
        _run(loop._ensure_action_index_built(
            idx, "provider", "standard", await_completion=True,
        ))
    assert idx.prepare_calls == 1, "memoized failure must prevent a retry"


def test_ready_gate_reflects_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: equivalence case 5 — is_ready() (both idx's own gate — used
    for search_actions visibility — and the Coordinator's parallel
    manifest-backed gate) is False before a build and True after."""
    ctx = _op_ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    loop = _LoopForP2b(tmp_path, ctx)
    idx = _FakeActionIndex()
    coordinator = loop._get_index_coordinator()

    assert idx.is_ready() is False
    assert _run(coordinator.is_ready("actions")) is False

    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))

    assert idx.is_ready() is True
    assert _run(coordinator.is_ready("actions")) is True


# ── 3. Mandatory strip-falsify co-vet gates (#3270 §5) ────────────────────


def test_self_deadlock_non_recurrence_two_consecutive_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: (strip-falsify, #3270 §5 gate 1 — mandatory) two consecutive
    same-process action-index builds through the PRODUCTION path
    (``ensure_built`` + ``register_builder``) — the 2nd does NOT silently
    skip (it correctly rebuilds: ``is_ready()`` True, no exception, real
    material actually written).

    Strip-falsify: a build_fn that reproduces the pre-PR1 bug — a SECOND
    ``try_acquire_build_lock`` acquisition at the SAME path the
    Coordinator's own ``_run_build`` already holds — silently no-ops the
    build (the second acquisition sees ITS OWN pid as a live holder and
    skips). This proves the fix (stripping ``ActionEmbeddingIndex``'s own
    lock) is load-bearing, not incidental: reintroducing the double-lock
    reproduces the exact bound-fired-but-silent hazard #3270 §2 removes
    by construction.
    """
    coord = IndexCoordinator(tmp_path)
    ctx = _op_ctx_for(_FakeEmbeddingProvider(), monkeypatch)

    # ── the FIXED shape: no lock inside build_fn (matches prepare_material) ──
    idx1 = _FakeActionIndex()

    async def _fixed_build_fn1() -> BuildMaterial | None:
        return await idx1.prepare_material(
            [{"id": "a", "text": "alpha"}], ctx, "standard",
        )

    coord.register_builder("actions", _fixed_build_fn1, kind="static")
    outcome1 = _run(coord.ensure_built("actions", await_completion=True))
    assert outcome1.error is None
    assert outcome1.chunk_count == 1
    assert idx1.prepare_calls == 1

    # A second build — force a re-check the way a fresh idx instance in a
    # new process/session would (see RouterLoop._ensure_action_index_built's
    # mark_dirty-when-Coordinator-clean-but-instance-not-ready path).
    _run(coord.mark_dirty("actions", reason="force_recheck"))
    idx2 = _FakeActionIndex()

    async def _fixed_build_fn2() -> BuildMaterial | None:
        return await idx2.prepare_material(
            [{"id": "a", "text": "alpha"}], ctx, "standard",
        )

    coord.register_builder("actions", _fixed_build_fn2, kind="static")
    outcome2 = _run(coord.ensure_built("actions", await_completion=True))
    assert outcome2.error is None
    assert idx2.prepare_calls == 1, (
        "the second build must actually run (not silently no-op) once "
        "marked dirty again"
    )
    assert outcome2.chunk_count == 1

    # ── the BUGGY (pre-PR1-shaped) shape: build_fn reacquires the SAME
    # advisory lock the Coordinator's own _run_build already holds ──────
    _run(coord.mark_dirty("actions", reason="force_recheck_buggy"))
    lock_dir = cache_dir_for_source(tmp_path, "actions")
    inner_probe: dict[str, Any] = {"ran": False, "got_lock": None, "produced_material": None}

    async def _buggy_double_locked_build_fn() -> BuildMaterial | None:
        # Reproduces the self-deadlock: a SECOND try_acquire_build_lock
        # at the exact path _run_build's own `with try_acquire_build_lock`
        # (coordinator.py:_run_build) already holds, in the SAME process.
        with try_acquire_build_lock(lock_dir) as got_lock:
            inner_probe["ran"] = True
            inner_probe["got_lock"] = got_lock
            if not got_lock:
                # This is the bug: the SAME pid is seen as the live
                # holder (itself, one call frame up) and the build
                # silently no-ops instead of running.
                inner_probe["produced_material"] = False
                return None
            inner_probe["produced_material"] = True
            return BuildMaterial(
                items=[{"id": "a", "text": "alpha"}],
                texts=["alpha"],
                to_chunk_record=_to_chunk_record,
                model_class="standard",
                ctx=ctx,
            )

    coord.register_builder("actions", _buggy_double_locked_build_fn, kind="static")
    outcome3 = _run(coord.ensure_built("actions", await_completion=True))
    assert inner_probe["ran"] is True, "sanity: the buggy build_fn actually ran"
    assert outcome3.error is None, "the double-lock bug does not raise — it silently no-ops"
    assert inner_probe["got_lock"] is False, (
        "RED (of the guarantee) is exactly this: the second try_acquire_build_lock "
        "acquisition, at the SAME path _run_build's own outer `with` already holds "
        "in this SAME process, sees its OWN pid as a live holder and reports "
        "got_lock=False — the self-deadlock-shaped silent-no-op #3270 §2's "
        "single-lock-holder fix removes by construction. The FIXED build_fn shapes "
        "above (no inner lock at all) never hit this branch."
    )
    assert inner_probe["produced_material"] is False, (
        "the double-locked build_fn silently produces NO material (its own "
        "got_lock=False fallback) even though a real rebuild was due — exactly "
        "the bound-fired-but-silent hazard: ensure_built itself reports "
        "error=None (success-shaped), masking that nothing was actually rebuilt."
    )


def test_embed_cost_duplicate_avoidance_cross_process_mid_build(tmp_path: Path) -> None:
    """Tier 2: (strip-falsify, #3270 §5 gate 2 — mandatory) a cross-process
    mid-build scenario (another live PID holds the advisory lock) →
    ``ensure_built`` falls back without invoking ``build_fn`` at all — NO
    duplicate embed-API cost.

    Strip-falsify: with the lock file staged BEFORE calling
    ``ensure_built``, the build_fn must NOT be invoked. This is the
    Coordinator-level proof that the SOLE lock acquisition (now living
    only in ``_run_build``, since ``ActionEmbeddingIndex`` no longer
    holds its own copy) still prevents the duplicate-embed-cost hazard
    the pre-PR1 two-lock design also prevented — a regression here would
    mean stripping the adapter's lock silently lost this guarantee
    instead of correctly centralizing it.
    """
    coord = IndexCoordinator(tmp_path)
    build_fn_calls = {"count": 0}

    async def _build_fn() -> BuildMaterial:
        build_fn_calls["count"] += 1
        return BuildMaterial(
            items=[{"id": "a", "text": "alpha"}],
            texts=["alpha"],
            to_chunk_record=_to_chunk_record,
            model_class="standard",
            ctx="unused",
        )

    coord.register_builder("actions", _build_fn, kind="static")

    # Stage: another (live) process holds the cross-process build lock.
    lock_dir = cache_dir_for_source(tmp_path, "actions")
    lock_path = lock_dir / ".build.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8",
    )
    try:
        outcome = _run(coord.ensure_built("actions", await_completion=True))
    finally:
        lock_path.unlink(missing_ok=True)

    assert build_fn_calls["count"] == 0, (
        "build_fn must NOT be invoked while another process holds the "
        "build lock — invoking it would duplicate the embed-API cost"
    )
    assert outcome.triggered is False


def test_register_builder_production_fail_closed_for_unregistered_source(
    tmp_path: Path,
) -> None:
    """Tier 2: (#3270 §5 gate 3 — mandatory) an unregistered source_id
    fails CLOSED (``ValueError``) rather than silently no-building —
    ``register_builder`` is now a PRODUCTION path (the action-catalog),
    not test-only, so a registration omission must be loud."""
    coord = IndexCoordinator(tmp_path)
    try:
        _run(coord.ensure_built("never_registered", await_completion=True))
        raise AssertionError("expected ValueError for an unregistered source")
    except ValueError as exc:
        assert "never_registered" in str(exc)
        assert "register_builder" in str(exc)
