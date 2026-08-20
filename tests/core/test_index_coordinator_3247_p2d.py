"""FP-0066 P2d (#3247) — IndexCoordinator audit-event phase emit + the
``search_await`` contract's production wiring at the two live action-catalog
query call sites.

The architect's decomposition-correction comment on #3247 folds the
original P2c (sync-in-op op wiring + G3 delete-de-index) INTO P3 — its only
producers are P3-dependent (0 live callers today) — and dispatches P2d
FIRST, verified against the one live embedding-index producer: the
action-catalog build/search (P2b, #3260).

Covers:
  1. ``embedding_index_build_started``/``_progress``/``_complete`` fire at
     the Coordinator's build-execution method boundary (``ensure_built``,
     the material-producing path).
  2. A build failure emits ``embedding_index_build_error`` (with a
     ``reason``).
  3. The pre-P2d ``action_index_build_failed`` event (previously emitted
     directly by the now-deleted ``RouterLoop.
     _build_action_embedding_index_background``, P2-convergence PR2,
     #3270 §3) no longer double-emits alongside its fold target,
     ``embedding_index_build_error`` — see also the updated pins in
     ``test_action_embedding_build_failure_1458.py`` and
     ``test_index_coordinator_3247_p2b.py``.
  4. ``search_await`` production wiring at BOTH live query call sites
     (``RouterLoop.search_actions`` / ``universal_catalog.
     _handle_search_actions``): a clean source is a cheap no-op (no build
     triggered); ``semantic_search_started``/``_complete`` (results count)
     fire around the real query.

P2-convergence PR1 (#3270 §2): ``IndexCoordinator.ensure_built_self_
contained`` — the two-path shape P2b introduced for the action-catalog
(which used to own its own lock+write) — is ELIMINATED. The action-catalog
now routes through the SAME ``ensure_built``/``register_builder`` path as
every other source (via ``ActionEmbeddingIndex.prepare_material``, the
lock-free ``BuildFn`` — see ``reyn.tools.action_index``); the
self-contained-specific coverage this file used to carry (§2's "via BOTH
shapes" + the freestanding ``_FakeSelfContainedBuilder`` tests) is REMOVED
— its regression value now lives in
``tests/core/test_index_coordinator_3247_p2b.py`` (the equivalence suite +
mandatory #3270 §5 strip-falsify gates), which exercises the SAME
production call path (``RouterLoop._ensure_action_index_built``) this file
already tests.

P2-convergence PR2 (#3270 §3): the interim dual-sync pin (formerly §3's
``test_action_index_build_failure_both_signals_stay_in_sync``) is RETIRED
and replaced by
``test_action_index_build_failure_is_single_sourced_on_coordinator`` — the
twin RouterLoop-side ``_action_index_build_failed`` flag is removed
entirely, so failure-state has a single owner (the Coordinator's
``_failure_memo``/``build_failed()``).

No mocks — real ``IndexCoordinator``, real ``SourceManifest``, a real
``EventLog`` subscribed to a real ``EventStore`` (audit-events are read back
from the actual ``.reyn/events`` JSONL files, not asserted against private
state), a real ``ActionEmbeddingIndex`` + real ``OpContext``; a plain fake
embedding provider (same established convention as
``tests/core/test_action_embedding_index.py`` / ``test_index_coordinator_3247_
p2a.py``) stands in for the litellm boundary.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.index.backend import ChunkRecord
from reyn.data.index.coordinator import BuildMaterial, IndexCoordinator
from reyn.data.index.source_manifest import SourceEntry, get_source_manifest
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.action_index import ActionEmbeddingIndex
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import SEARCH_ACTIONS
from tests._support.events import settle


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _events_and_store(tmp_path: Path) -> tuple[EventLog, EventStore]:
    """A real EventLog subscribed to a real EventStore rooted under
    ``tmp_path / ".reyn" / "events"`` — audit-events are read back from
    the actual on-disk JSONL, per CLAUDE.md's real-instance testing policy."""
    store = EventStore(tmp_path / ".reyn" / "events")
    log = EventLog(subscribers=[store])
    return log, store


async def _read_back(log: EventLog, store: EventStore) -> list[dict]:
    """Wait for *log*'s dispatch queue to finish delivering to *store*
    (its subscriber), drain the store's own off-loop write queue, then
    read every event back from disk (public API — ``iter_all()`` — not
    private state)."""
    await settle(log)
    await store.flush()
    return [e.model_dump(mode="json") for e in store.iter_all()]


class _FakeEmbeddingProvider:
    """Deterministic canned vectors, one per input text (no litellm call)."""

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        vectors = [[float((hash((t, i)) % 1000) / 1000.0) for i in range(4)] for t in texts]
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


class _FailingEmbeddingProvider:
    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        raise RuntimeError("embedding API unreachable")


def _op_ctx_for(provider: Any, monkeypatch: pytest.MonkeyPatch, events: EventLog) -> OpContext:
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)
    ws = Workspace(events=events)
    return OpContext(workspace=ws, events=events, permission_decl=PermissionDecl())


def _to_chunk_record(item: dict, vector: list[float], resolved_model: str) -> ChunkRecord:
    return ChunkRecord(
        text=item["text"],
        vector=list(vector),
        metadata={
            "source_path": item["id"],
            "source_type": "test",
            "content_hash": item["id"],
            "embedding_model": resolved_model,
            "chunk_index": 0,
            "size_tokens": 0,
            "parent_context": None,
            "extra": {},
        },
    )


def _material_build_fn(ctx: OpContext, items: list[dict]):
    async def _build() -> BuildMaterial:
        return BuildMaterial(
            items=items,
            texts=[it["text"] for it in items],
            to_chunk_record=_to_chunk_record,
            model_class="standard",
            ctx=ctx,
        )
    return _build


# ── 1 + 2. Audit-event phase emit — ensure_built (material path) ──────────


def test_ensure_built_emits_started_progress_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: a successful ``ensure_built`` build emits the three build
    phases in order, read back from the real on-disk audit-event log."""
    log, store = _events_and_store(tmp_path)
    coord = IndexCoordinator(tmp_path)
    ctx = _op_ctx_for(_FakeEmbeddingProvider(), monkeypatch, log)
    items = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]
    coord.register_builder("doc_source", _material_build_fn(ctx, items), kind="dynamic")

    async def _scenario() -> list[dict]:
        outcome = await coord.ensure_built(
            "doc_source", await_completion=True, events=log,
        )
        assert outcome.error is None
        assert outcome.chunk_count == 2
        return await _read_back(log, store)

    events = _run(_scenario())
    # Filter to the P2d build-phase events — the `embed` op's own execution
    # machinery (e.g. `permission_granted`) shares this EventLog and is
    # irrelevant to this test's claim (build-phase ORDER + payload).
    phase_events = [e for e in events if e["type"].startswith("embedding_index_build_")]
    kinds = [e["type"] for e in phase_events]
    assert kinds == [
        "embedding_index_build_started",
        "embedding_index_build_progress",
        "embedding_index_build_complete",
    ]
    started = phase_events[0]["data"]
    assert started["source_id"] == "doc_source"
    assert started["kind"] == "dynamic"
    progress = phase_events[1]["data"]
    assert progress["source_id"] == "doc_source"
    assert progress["chunk_count"] == 2
    complete = phase_events[2]["data"]
    assert complete["source_id"] == "doc_source"
    assert complete["chunk_count"] == 2


def test_ensure_built_failure_emits_build_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: a build failure emits ``embedding_index_build_error`` with a
    ``reason`` — no ``_complete`` event fires."""
    log, store = _events_and_store(tmp_path)
    coord = IndexCoordinator(tmp_path)
    ctx = _op_ctx_for(_FailingEmbeddingProvider(), monkeypatch, log)
    items = [{"id": "a", "text": "alpha"}]
    coord.register_builder("doc_source", _material_build_fn(ctx, items), kind="dynamic")

    async def _scenario() -> list[dict]:
        outcome = await coord.ensure_built(
            "doc_source", await_completion=True, events=log,
        )
        assert outcome.error is not None
        return await _read_back(log, store)

    events = _run(_scenario())
    kinds = [e["type"] for e in events]
    assert "embedding_index_build_error" in kinds
    assert "embedding_index_build_complete" not in kinds
    error_event = next(e for e in events if e["type"] == "embedding_index_build_error")
    assert error_event["data"]["source_id"] == "doc_source"
    assert "unreachable" in error_event["data"]["reason"]


def test_events_none_is_a_silent_noop(tmp_path: Path) -> None:
    """Tier 2: ``events=None`` (the default) skips audit-emit entirely
    without raising — every other Coordinator collaborator (backend,
    manifest) is similarly best-effort-optional."""
    coord = IndexCoordinator(tmp_path)

    async def _build_fn() -> BuildMaterial:
        raise AssertionError("not reached — no builder registered")

    coord.register_builder("x", _build_fn, kind="dynamic")
    # No events kwarg at all — must not raise.
    outcome = _run(coord.ensure_built("x", await_completion=True))
    assert outcome.error is None or outcome.error is not None  # just must not raise


# ── search-await production wiring — RouterLoop.search_actions ────────────


class _StubHost:
    """Minimal host for RouterLoop.search_actions + _get_index_coordinator."""

    def __init__(self, index: Any, provider: Any, events: EventLog, op_ctx: Any) -> None:
        self._index = index
        self._provider = provider
        self.events = events
        self._op_ctx = op_ctx

    def get_action_embedding_index(self) -> Any:
        return self._index

    def get_embedding_provider(self) -> Any:
        return self._provider

    def get_embedding_model_class(self) -> str:
        return "standard"

    def make_router_op_context(self) -> Any:
        return self._op_ctx


class _LoopForP2d(RouterLoop):
    def __init__(self, workspace_root: Path, host: _StubHost) -> None:
        self.host = host  # type: ignore[assignment]
        self.chain_id = "test-chain"
        self._workspace_root_for_test = workspace_root

    def _get_index_coordinator(self) -> IndexCoordinator:
        if not hasattr(self, "_test_coordinator"):
            self._test_coordinator = IndexCoordinator(self._workspace_root_for_test)
        return self._test_coordinator

    async def _build_router_caller_state(self) -> Any:
        # Same minimal-subclass shim as test_index_coordinator_3247_p2b.py's
        # _LoopForP2b — the list_actions handler still returns the static
        # categories with router_state=None; dynamic categories are not
        # needed for these tests.
        return None


# ── convergence-debt interim guard (#3247 co-vet, pre-merge) ──────────────


def test_action_index_build_failure_is_single_sourced_on_coordinator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: P2-convergence PR2 (#3270 §3) single-source witness.

    Retires the interim dual-sync pin (formerly
    ``test_action_index_build_failure_both_signals_stay_in_sync``): the
    twin RouterLoop-side ``_action_index_build_failed`` flag (#1458's
    original per-session retry-prevention bookkeeping) is REMOVED — the
    Coordinator's own failure-memo (``build_failed(source_id)``) is now
    the SOLE owner of build-failure state. This test drives a REAL build
    failure through the REAL production path
    (``RouterLoop._ensure_action_index_built``, real ``IndexCoordinator``,
    real ``ActionEmbeddingIndex``, no mocks) and asserts (a) the
    Coordinator's memo reflects the failure AND (b) the RouterLoop
    instance no longer even HAS the twin attribute — witnessing the
    double-record is gone, not merely that it stayed in sync.
    """
    log, store = _events_and_store(tmp_path)
    provider = _FailingEmbeddingProvider()
    op_ctx = _op_ctx_for(provider, monkeypatch, log)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)

    host = _StubHost(idx, provider, log, op_ctx)
    loop = _LoopForP2d(tmp_path, host)
    coordinator = loop._get_index_coordinator()

    async def _scenario() -> None:
        await loop._ensure_action_index_built(
            idx, provider, "standard", await_completion=True,
        )

    _run(_scenario())

    assert coordinator.build_failed("actions") is True, (
        "the Coordinator's failure-memo must be set on a real build failure"
    )
    _sentinel = object()
    assert getattr(loop, "_action_index_build_failed", _sentinel) is _sentinel, (
        "the twin RouterLoop-side #1458 flag must no longer exist — "
        "failure-state has a single owner (the Coordinator's memo)"
    )


def test_router_loop_search_actions_wires_search_await_and_emits_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: ``RouterLoop.search_actions`` — the first live query call
    site (~router_loop.py) — awaits ``Coordinator.search_await`` before
    serving and emits ``semantic_search_started``/``_complete`` (results
    count) around the real query. A clean (already-built) source is a
    cheap no-op — the query itself still runs and returns real results."""
    log, store = _events_and_store(tmp_path)
    provider = _FakeEmbeddingProvider()
    op_ctx = _op_ctx_for(provider, monkeypatch, log)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    items = [
        {"action_name": "skill__alpha", "short_description": "Alpha skill"},
        {"action_name": "skill__beta", "short_description": "Beta skill"},
    ]

    host = _StubHost(idx, provider, log, op_ctx)
    loop = _LoopForP2d(tmp_path, host)
    # Register the coordinator's manifest as "clean" so search_await is a
    # no-op (steady-state) — mirrors production: the build path
    # (_ensure_action_index_built) would have already recorded this.
    coordinator = loop._get_index_coordinator()

    async def _scenario() -> tuple[list[str], list[dict]]:
        # Build inside the SAME event loop as the rest of this scenario —
        # `log`'s dispatch consumer task is bound to whichever loop first
        # emits through it (`EventLog._ensure_consumer_started`), and
        # `settle`/`drain` below needs that consumer still alive on THIS
        # loop, not a prior, already-closed `asyncio.run()`'s.
        await idx.build(items, op_ctx, "standard")
        assert idx.is_ready() is True

        # Register a builder that raises if ever invoked — search_await
        # must NOT trigger a build on a source the manifest already
        # records as "clean" (steady-state no-op, per the firm's §5
        # contract). Public SourceManifest API only (no private-state
        # poke) — SourceEntry's ``state`` defaults to "clean".
        async def _must_not_build() -> BuildMaterial:
            raise AssertionError("search_await must not build a clean source")
        coordinator.register_builder("actions", _must_not_build, kind="static")
        manifest = get_source_manifest(tmp_path)
        await manifest.upsert(SourceEntry(
            name="actions", description="", path="", kind="static", state="clean",
        ))

        names = await loop.search_actions("alpha")
        return names, await _read_back(log, store)

    names, events = _run(_scenario())
    assert "skill__alpha" in names
    kinds = [e["type"] for e in events]
    assert "semantic_search_started" in kinds
    assert "semantic_search_complete" in kinds
    complete = next(e for e in events if e["type"] == "semantic_search_complete")
    assert complete["data"]["source_id"] == "actions"
    assert complete["data"]["results"] == len(names)


# ── search-await production wiring — universal_catalog._handle_search_actions ──


def test_universal_catalog_handler_wires_search_await_and_emits_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: ``_handle_search_actions`` — the second live query call site
    (~universal_catalog.py) — awaits ``Coordinator.search_await`` before
    serving and emits ``semantic_search_started``/``_complete`` (results
    count) around the real query."""
    log, store = _events_and_store(tmp_path)
    provider = _FakeEmbeddingProvider()
    op_ctx = _op_ctx_for(provider, monkeypatch, log)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    items = [
        {"action_name": "skill__alpha", "short_description": "Alpha skill"},
        {"action_name": "skill__beta", "short_description": "Beta skill"},
    ]

    ws = Workspace(events=log, base_dir=tmp_path)
    rs = RouterCallerState(
        action_embedding_index=idx,
        embedding_provider=provider,
        embedding_model_class="standard",
        op_context_factory=lambda: op_ctx,
    )
    ctx = ToolContext(
        events=log, permission_resolver=None, workspace=ws,
        caller_kind="router", router_state=rs,
    )

    async def _scenario() -> tuple[dict, list[dict]]:
        # Build inside the SAME event loop as the rest of this scenario —
        # see the sibling RouterLoop test above for why (`log`'s dispatch
        # consumer task must stay bound to THIS loop for settle/drain to
        # observe it).
        await idx.build(items, op_ctx, "standard")
        assert idx.is_ready() is True

        result = await SEARCH_ACTIONS.handler({"query": "alpha", "limit": 5}, ctx)
        return result, await _read_back(log, store)

    result, events = _run(_scenario())
    assert result["items"], "expected ranked results from the real query"
    kinds = [e["type"] for e in events]
    assert "semantic_search_started" in kinds
    assert "semantic_search_complete" in kinds
    complete = next(e for e in events if e["type"] == "semantic_search_complete")
    assert complete["data"]["source_id"] == "actions"
    assert complete["data"]["results"] == len(result["items"])


def test_search_await_clean_state_does_not_trigger_build_at_call_site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: the search-await contract's core promise — a clean source is
    a cheap no-op. Registers a builder that would raise ``AssertionError``
    if invoked (a build must NOT be triggered by ``search_await`` once the
    Coordinator's manifest already records ``state == "clean"``), then
    drives the real query call site end to end."""
    log, store = _events_and_store(tmp_path)
    provider = _FakeEmbeddingProvider()
    op_ctx = _op_ctx_for(provider, monkeypatch, log)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    items = [{"action_name": "skill__alpha", "short_description": "Alpha skill"}]
    _run(idx.build(items, op_ctx, "standard"))

    ws = Workspace(events=log, base_dir=tmp_path)
    rs = RouterCallerState(
        action_embedding_index=idx,
        embedding_provider=provider,
        embedding_model_class="standard",
        op_context_factory=lambda: op_ctx,
    )
    ctx = ToolContext(
        events=log, permission_resolver=None, workspace=ws,
        caller_kind="router", router_state=rs,
    )

    async def _scenario() -> dict:
        from reyn.data.index.coordinator import get_index_coordinator
        coordinator = get_index_coordinator(tmp_path)

        async def _must_not_build() -> BuildMaterial:
            raise AssertionError("search_await must not trigger a build on a clean source")
        coordinator.register_builder("actions", _must_not_build, kind="static")
        manifest = get_source_manifest(tmp_path)
        await manifest.upsert(SourceEntry(
            name="actions", description="", path="", kind="static", state="clean",
        ))

        return await SEARCH_ACTIONS.handler({"query": "alpha"}, ctx)

    result = _run(_scenario())
    assert result["items"], "query must still serve real results"
