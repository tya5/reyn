"""FP-0066 P2b (#3247) — per-kind source split + action-catalog migration.

Covers:
  1. ``SourceEntry.kind`` taxonomy — persist + coerce-default ("backfill").
  2. ``IndexCoordinator.ensure_built_self_contained`` — the orchestration-
     only entry point ``RouterLoop._ensure_action_index_built`` uses to
     migrate the action-catalog build (which owns its OWN cross-process
     lock + disk-adopt + dual-axis invalidation policy, so it cannot use
     the material-producing ``ensure_built`` without double-acquiring the
     lock — see ``coordinator.py``'s docstring on that method).
  3. **Equivalence test (mandatory)**: the migrated
     ``RouterLoop._ensure_action_index_built`` path reproduces the
     pre-migration observable behavior for the 5 pinned cases —
     eager→sync build, non-eager→background, disk-adopt hit→no rebuild,
     build failure→memoized (not re-attempted), ready-gate reflects state.

No mocks — real ``IndexCoordinator``, real ``SourceManifest``, real
``RouterLoop`` instances (constructed the same minimal-subclass way
``tests/test_action_embedding_build_failure_1458.py`` already does, since
a full host/session is not needed to exercise this orchestration layer);
a plain fake index (real class, not a Mock) stands in for
``ActionEmbeddingIndex`` so build success/failure/timing is controllable
without a real embedding provider.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reyn.data.index.coordinator import IndexCoordinator
from reyn.data.index.source_manifest import SourceEntry, SourceManifest
from reyn.runtime.router_loop import RouterLoop


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


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
    """Tier 2: mark_dirty on a never-seen source_id tags the NEW entry with
    the kind remembered from register_builder/ensure_built_self_contained,
    not the "backfill" dataclass default. Reads back through a SEPARATE
    ``SourceManifest`` instance against the same file SSoT (public surface
    only — no private-state introspection)."""
    manifest = SourceManifest(tmp_path)
    coord = IndexCoordinator(tmp_path, manifest=manifest)

    async def _build() -> None:
        return None

    _run(coord.ensure_built_self_contained(
        "actions", _build, await_completion=True,
        is_ready_probe=lambda: False, kind="static",
    ))
    entry = _run(manifest.get("actions"))
    assert entry is not None
    assert entry.kind == "static"


# ── 2 + 3. ensure_built_self_contained + the 5-case equivalence set ──────


class _FakeActionIndex:
    """Real fake standing in for ActionEmbeddingIndex — controllable
    success/failure/timing, no embedding provider needed."""

    def __init__(self, *, should_fail: bool = False, delay: float = 0.0) -> None:
        self.should_fail = should_fail
        self.delay = delay
        self.build_calls = 0
        self._ready = False
        self._size = 0
        self.source_name = "actions"

    def is_ready(self) -> bool:
        return self._ready

    def size(self) -> int:
        return self._size

    async def build(self, items: list[dict], ctx: Any, model_class: str) -> None:
        self.build_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.should_fail:
            raise RuntimeError("simulated provider failure")
        self._ready = True
        self._size = len(items)


class _StubHost:
    """Minimal host — only what RouterLoop._ensure_action_index_built /
    _build_action_embedding_index_background touch."""

    def __init__(self) -> None:
        self.events = _StubEvents()
        self.op_ctx_stub: Any = "ctx"

    def make_router_op_context(self) -> Any:
        return self.op_ctx_stub


class _StubEvents:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, kind: str, **kwargs: Any) -> None:
        self.emitted.append({"kind": kind, **kwargs})


class _LoopForP2b(RouterLoop):
    """RouterLoop subclass exercising the P2b orchestration methods without
    a full host/session — same minimal-subclass pattern as
    ``test_action_embedding_build_failure_1458.py``."""

    def __init__(self, workspace_root: Path) -> None:
        self.host = _StubHost()  # type: ignore[assignment]
        self.chain_id = "test-chain"
        self._workspace_root_for_test = workspace_root

    async def _build_router_caller_state(self) -> Any:
        return None

    def _get_index_coordinator(self) -> IndexCoordinator:
        # Deterministic per-test coordinator instance (bypasses the
        # module singleton so tests don't leak state across each other).
        if not hasattr(self, "_test_coordinator"):
            self._test_coordinator = IndexCoordinator(self._workspace_root_for_test)
        return self._test_coordinator


def test_eager_awaits_sync_build(tmp_path: Path) -> None:
    """Tier 2: equivalence case 1 — eager (await_completion=True) runs the
    build INLINE — by the time the call returns, the index is ready."""
    loop = _LoopForP2b(tmp_path)
    idx = _FakeActionIndex()
    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    assert idx.is_ready() is True
    assert idx.build_calls == 1
    coordinator = loop._get_index_coordinator()
    assert _run(coordinator.is_ready("actions")) is True


def test_non_eager_schedules_background(tmp_path: Path) -> None:
    """Tier 2: equivalence case 2 — non-eager (await_completion=False)
    schedules the build in the BACKGROUND — the call returns before the
    (delayed) build completes, and the index becomes ready only once the
    background task finishes."""
    loop = _LoopForP2b(tmp_path)
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
            if await coordinator.is_ready("actions"):
                break
            await asyncio.sleep(0.01)
        assert idx.is_ready() is True
        assert await coordinator.is_ready("actions") is True

    _run(_scenario())


def test_disk_adopt_hit_skips_rebuild(tmp_path: Path) -> None:
    """Tier 2: equivalence case 3 — once the manifest already records
    state=="clean" AND the domain adapter itself reports ready (the
    Coordinator-level analogue of a disk-adopt cache hit — a fresh
    process/instance that finds a completed prior build), a second
    ``ensure_built_self_contained`` call does NOT re-invoke the builder."""
    loop = _LoopForP2b(tmp_path)
    idx = _FakeActionIndex()
    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    assert idx.build_calls == 1

    # Second call — index is still ready (nothing invalidated it) — must
    # be a no-op (no re-embed cost).
    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    assert idx.build_calls == 1, "a clean+ready source must not rebuild"


def test_build_failure_memoized_not_reattempted(tmp_path: Path) -> None:
    """Tier 2: equivalence case 4 — a build failure is memoized on BOTH the
    RouterLoop-side flag (#1458, unchanged) AND the Coordinator's own
    failure-memo (``build_failed``) — and the production retry guard
    (checked by the caller before invoking again) prevents a second
    attempt."""
    loop = _LoopForP2b(tmp_path)
    idx = _FakeActionIndex(should_fail=True)

    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    assert idx.build_calls == 1
    assert getattr(loop, "_action_index_build_failed", False) is True
    coordinator = loop._get_index_coordinator()
    assert coordinator.build_failed("actions") is True

    # Production retry guard (mirrors RouterLoop.run()'s own gate): do NOT
    # call again once the failure flag is set.
    if not getattr(loop, "_action_index_build_failed", False):
        _run(loop._ensure_action_index_built(
            idx, "provider", "standard", await_completion=True,
        ))
    assert idx.build_calls == 1, "memoized failure must prevent a retry"


def test_ready_gate_reflects_state(tmp_path: Path) -> None:
    """Tier 2: equivalence case 5 — is_ready() (both idx's own gate — used
    for search_actions visibility — and the Coordinator's parallel
    manifest-backed gate) is False before a build and True after."""
    loop = _LoopForP2b(tmp_path)
    idx = _FakeActionIndex()
    coordinator = loop._get_index_coordinator()

    assert idx.is_ready() is False
    assert _run(coordinator.is_ready("actions")) is False

    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))

    assert idx.is_ready() is True
    assert _run(coordinator.is_ready("actions")) is True


def test_1458_pinned_build_primitive_still_used_unchanged(tmp_path: Path) -> None:
    """Tier 2: the P2b migration routes THROUGH
    ``_build_action_embedding_index_background`` (#1458's pinned failure-
    memoization/log primitive) rather than bypassing it — regression pin
    that the migration didn't orphan that method into dead code reachable
    only from its own unit test.

    FP-0066 P2d (#3247 firm §6) updated this pin's audit-event assertion:
    the primitive's OWN direct ``action_index_build_failed`` emit was
    folded into ``embedding_index_build_error``, now emitted by
    ``IndexCoordinator.ensure_built_self_contained`` (one layer up, reached
    via ``_ensure_action_index_built``) — so a failure surfaces the NEW
    event name here, and the OLD name must not double-emit."""
    loop = _LoopForP2b(tmp_path)
    idx = _FakeActionIndex(should_fail=True)
    _run(loop._ensure_action_index_built(idx, "provider", "standard", await_completion=True))
    kinds = [e["kind"] for e in loop.host.events.emitted]
    assert "embedding_index_build_error" in kinds, (
        "the folded embedding_index_build_error event must fire via "
        "IndexCoordinator.ensure_built_self_contained on a build failure"
    )
    assert "action_index_build_failed" not in kinds, (
        "the pre-P2d action_index_build_failed event must not double-emit "
        "alongside its P2d fold target"
    )
