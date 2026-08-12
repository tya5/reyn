"""FP-0066 P3b (#3247) — repo_doc/repo_src knowledge ingest (static/
background) + doc/src classification + content_hash reconcile + §G3
source-unit de-index via ``index_drop`` + dirty->heal recovery.

Covers (per the architect's P3 firm §1/§3/§4 + the P3b task brief):
  1. **static/background, non-blocking**: ``sync_repo_ingest_background``
     schedules BOTH sources' builds as background tasks — the state right
     after the triggering call returns must NOT already be "clean" (proof
     the caller did not block on the actual embed/write work), and a
     subsequent ``search_await`` (the public completion-await surface)
     brings both to "clean" with the expected content.
  2. **doc/src classification**: a ``.md`` file (README.md, docs/*.md,
     including a ``*.ja.md``) ingests into ``knowledge_repo_doc``; a code
     file (``src/**/*.py``) ingests into ``knowledge_repo_src`` — disjoint,
     v1 extension-based per the brief.
  3. **content_hash reconcile**: a changed file's next build reflects the
     new text; a removed file's row is gone after the next build (both via
     the SAME full-replace-per-build primitive memory/skill already use —
     no live filesystem watcher).
  4. **§G3 source-unit de-index**: the existing ``index_drop`` op (whole-
     source drop, already live per FP-0066 P1b) applied to
     ``knowledge_repo_doc`` removes EVERY entity that source holds while
     leaving ``knowledge_repo_src`` untouched — the firm's §4 ruling that
     repo de-index is source-unit, not per-file.
  5. **recovery**: a background build fault (provider failure) leaves the
     source ``dirty``; a later ``search_await`` with a working provider
     heals it to ``clean`` (P2a's recovery machinery, same shape P3a's
     real-producer test exercised for memory/skill).

No mocks — real ``SqliteIndexBackend``, real ``SourceManifest``, real
``IndexCoordinator``/``OpContext``/``Workspace``; a plain
``_FakeEmbeddingProvider`` (established convention, see
``tests/core/test_fp0066_p3a_knowledge_ingest.py``) stands in for the litellm
boundary via the ``get_provider`` monkeypatch seam. The repo CONTENT
itself is a synthetic fake tree under ``tmp_path`` (``resolve_reyn_root``
monkeypatched to it) rather than this actual checkout — deterministic,
fast, and immune to this repo's own file churn.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.index import SqliteIndexBackend
from reyn.data.index.coordinator import IndexCoordinator
from reyn.data.index.knowledge_ingest import (
    KNOWLEDGE_REPO_DOC_SOURCE_ID,
    KNOWLEDGE_REPO_SRC_SOURCE_ID,
    _repo_build_fn,
    repo_content_hash,
    sync_repo_ingest_background,
)
from reyn.data.index.source_manifest import get_source_manifest
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import IndexDropIROp
from reyn.security.permissions.permissions import PermissionDecl
from tests._support.events import collect_events


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeEmbeddingProvider:
    """Deterministic canned vectors, one per input text — no litellm call.
    Mirrors ``tests/core/test_fp0066_p3a_knowledge_ingest.py::_FakeEmbeddingProvider``."""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.calls.append(tuple(texts))
        if self.delay:
            # A REAL suspension point (unlike a synchronous compute-only
            # fake) — makes the "did the caller block on the actual embed
            # work" timing assertion in test 1 unambiguous: if the caller
            # blocked, its own await would observe this sleep; if it
            # merely scheduled a background task, its await returns long
            # before this sleep elapses.
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider unreachable (simulated)")
        vectors = [[float((hash((t, i)) % 1000) / 1000.0) for i in range(4)] for t in texts]
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)

    def _enabled() -> bool:
        return True
    monkeypatch.setattr(_embed_mod, "_is_embedding_enabled", _enabled)


def _patch_embedding_config_enabled(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """``sync_repo_ingest_background``'s own scheduling short-circuit reads
    ``load_config().embedding.enabled`` directly (not the op_runtime.embed
    private gate) — patched at the ``reyn.config`` package attribute (what
    ``_embedding_enabled``'s ``from reyn.config import load_config``
    actually resolves at call time) so a test can control it."""
    import reyn.config as _config_mod

    cfg = _config_mod.load_config()
    monkeypatch.setattr(cfg.embedding, "enabled", enabled, raising=False)
    monkeypatch.setattr(_config_mod, "load_config", lambda *a, **kw: cfg)


def _make_op_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(workspace=ws, events=events, permission_decl=PermissionDecl())


def _make_fake_repo(root: Path) -> None:
    """A tiny synthetic {README.md, docs/x.md, docs/y.ja.md, src/pkg/mod.py}
    tree — one file per doc/src bucket plus a `.ja.md` doc variant."""
    (root / "docs").mkdir(parents=True)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "README.md").write_text("# Hello\nrepo readme body v1\n", encoding="utf-8")
    (root / "docs" / "x.md").write_text("doc x body v1\n", encoding="utf-8")
    (root / "docs" / "y.ja.md").write_text("ja doc body v1\n", encoding="utf-8")
    (root / "src" / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")


def _patch_repo_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    import reyn.runtime.reyn_repo as _rr_mod
    monkeypatch.setattr(_rr_mod, "resolve_reyn_root", lambda: root)


async def _await_scheduled_source_build(
    coordinator: IndexCoordinator, manifest: Any, source_id: str,
) -> None:
    """Deterministically wait for a background-scheduled build to reach its
    final manifest state — no wall-clock sleep, no elapsed-time threshold
    (#3594).

    ``ensure_built(await_completion=False)`` (what
    ``sync_repo_ingest_background`` calls) schedules ``asyncio.create_task``
    and returns before the task has had a single event-loop tick to run —
    the manifest entry does not exist yet, so ``search_await`` (the public
    completion-await surface) would see "no entry" and take its no-op
    branch, never actually awaiting anything. The loop below gives the
    scheduled task event-loop ticks until the manifest entry exists
    (``_run_build``'s first line is ``await self._set_state(source_id,
    "building")``, so the entry appears as soon as the task gets to run at
    all); ``search_await`` then hands off to the coordinator's own
    ``_bg_tasks`` await, which is the actual completion signal.

    #3748: unbounded (owner policy) -- in scope despite being labeled "a
    safety net, not a timing budget", because a broken scheduler's failure
    DOES land on pass/fail: the old bound fell through to search_await's
    no-op branch, which let a caller's own ``assert state == "clean"`` go
    red -- but that red MISIDENTIFIES the cause (the scheduler never ran,
    not "the build never reached clean"). A hang's kill stack instead
    shows this exact ``while await manifest.get(...) is None``, naming the
    real cause directly -- strictly more precise than the red it replaces,
    not merely equally safe. (Contrast #3756 site 3, correctly left out of
    scope: there, timing out vs. not changes nothing -- pass/fail reaches
    the same way either branch goes.)

    ``sleep(0.01)`` not ``sleep(0)``: unbounded + a pure scheduler yield
    hot-spins one core for the life of a genuine hang, starving ``-n auto``
    siblings for the whole CI kill window; a real delay costs nothing once
    the predicate is already true (normally within one tick)."""
    while await manifest.get(source_id) is None:
        await asyncio.sleep(0.01)
    await coordinator.search_await(source_id)


# ── 1. static/background, non-blocking ──────────────────────────────────


def test_sync_repo_ingest_background_does_not_block_the_triggering_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: `sync_repo_ingest_background` schedules both repo_doc/
    repo_src builds as background tasks and returns WITHOUT waiting for
    the actual embed work — proved with a provider that sleeps on every
    `embed()` call (a REAL suspension point): the triggering call's own
    elapsed time must be far shorter than the provider's delay (if it had
    blocked, its own await would observe the sleep). A later
    `search_await` on each source completes the (still in-flight or not-
    yet-started) build and reaches "clean" (§8 — an unaware/per-turn
    caller must never foreground-block on an embedding build)."""
    repo_root = tmp_path / "fake_repo"
    _make_fake_repo(repo_root)
    _patch_repo_root(monkeypatch, repo_root)
    provider = _FakeEmbeddingProvider(fail=False, delay=0.3)
    _patch_provider(monkeypatch, provider)
    _patch_embedding_config_enabled(monkeypatch, enabled=True)

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    coordinator = IndexCoordinator(ws_root)
    op_ctx = _make_op_ctx(ws_root)
    events = EventLog()

    async def _trigger_then_wait() -> tuple[list[tuple[str, ...]], Any, Any]:
        manifest = get_source_manifest(ws_root)
        await sync_repo_ingest_background(coordinator, op_ctx, events=events)
        # The caller must not have blocked on the embed work. If it had,
        # ``provider.embed`` would already have been called by the time this
        # await returns — checked directly on the provider's own call log,
        # never on an elapsed-time threshold (#3594: a wall-clock bound here
        # produced false failures under CI load, since "scheduled but not
        # yet run" and "genuinely slow to schedule" look identical to a
        # timer but NOT to this call-log check).
        calls_right_after_trigger = list(provider.calls)
        await _await_scheduled_source_build(coordinator, manifest, KNOWLEDGE_REPO_DOC_SOURCE_ID)
        await _await_scheduled_source_build(coordinator, manifest, KNOWLEDGE_REPO_SRC_SOURCE_ID)
        return (
            calls_right_after_trigger,
            await manifest.get(KNOWLEDGE_REPO_DOC_SOURCE_ID),
            await manifest.get(KNOWLEDGE_REPO_SRC_SOURCE_ID),
        )

    calls_right_after_trigger, doc_entry, src_entry = _run(_trigger_then_wait())

    assert calls_right_after_trigger == [], (
        f"sync_repo_ingest_background's own await already reached the embed "
        f"provider ({calls_right_after_trigger!r}) — it must have blocked "
        "on the embed work instead of only scheduling a background task (§8)"
    )
    assert doc_entry is not None and doc_entry.state == "clean"
    assert src_entry is not None and src_entry.state == "clean"
    # README.md + docs/x.md + docs/y.ja.md = 3 doc entities.
    assert doc_entry.chunk_count == 3
    # src/pkg/mod.py = 1 src entity.
    assert src_entry.chunk_count == 1


def test_sync_repo_ingest_background_noops_when_embedding_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: when `embedding.enabled` is false, the scheduling call is a
    pure no-op (no manifest entry created at all) rather than repeatedly
    spawning a doomed-to-fail background build every turn."""
    repo_root = tmp_path / "fake_repo"
    _make_fake_repo(repo_root)
    _patch_repo_root(monkeypatch, repo_root)
    _patch_embedding_config_enabled(monkeypatch, enabled=False)

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    coordinator = IndexCoordinator(ws_root)
    op_ctx = _make_op_ctx(ws_root)

    _run(sync_repo_ingest_background(coordinator, op_ctx, events=None))

    manifest = get_source_manifest(ws_root)
    assert _run(manifest.get(KNOWLEDGE_REPO_DOC_SOURCE_ID)) is None
    assert _run(manifest.get(KNOWLEDGE_REPO_SRC_SOURCE_ID)) is None


# ── 2. doc/src classification ────────────────────────────────────────────


def test_doc_and_src_files_classify_and_ingest_disjointly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: a `.md` file (README.md/docs/*.md/*.ja.md) ingests into
    knowledge_repo_doc; a `.py` file ingests into knowledge_repo_src — the
    two sources' content is disjoint, v1 extension-based classification."""
    repo_root = tmp_path / "fake_repo"
    _make_fake_repo(repo_root)
    _patch_repo_root(monkeypatch, repo_root)
    provider = _FakeEmbeddingProvider(fail=False)
    _patch_provider(monkeypatch, provider)

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    coordinator = IndexCoordinator(ws_root)
    op_ctx = _make_op_ctx(ws_root)

    coordinator.register_builder(
        KNOWLEDGE_REPO_DOC_SOURCE_ID, _repo_build_fn("doc", op_ctx, "standard"), kind="static",
    )
    coordinator.register_builder(
        KNOWLEDGE_REPO_SRC_SOURCE_ID, _repo_build_fn("src", op_ctx, "standard"), kind="static",
    )
    _run(coordinator.ensure_built(KNOWLEDGE_REPO_DOC_SOURCE_ID, await_completion=True))
    _run(coordinator.ensure_built(KNOWLEDGE_REPO_SRC_SOURCE_ID, await_completion=True))

    backend = SqliteIndexBackend(workspace_root=ws_root)
    doc_hashes = _run(backend.existing_hashes(KNOWLEDGE_REPO_DOC_SOURCE_ID))
    src_hashes = _run(backend.existing_hashes(KNOWLEDGE_REPO_SRC_SOURCE_ID))

    assert repo_content_hash("doc", "README.md") in doc_hashes
    assert repo_content_hash("doc", "docs/x.md") in doc_hashes
    assert repo_content_hash("doc", "docs/y.ja.md") in doc_hashes
    assert repo_content_hash("src", "src/pkg/mod.py") in src_hashes
    # Disjoint: nothing crosses buckets.
    assert doc_hashes.isdisjoint(src_hashes)
    assert repo_content_hash("src", "src/pkg/mod.py") not in doc_hashes
    assert repo_content_hash("doc", "README.md") not in src_hashes


# ── 3. content_hash reconcile (add/update/remove via full-replace) ──────


def test_repo_build_reconciles_changed_and_removed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: a changed file's next build reflects the new text; a
    removed file's row is gone after the next build — both handled by the
    full-replace-per-build primitive already in `sqlite.py` (no live
    filesystem watcher)."""
    repo_root = tmp_path / "fake_repo"
    _make_fake_repo(repo_root)
    _patch_repo_root(monkeypatch, repo_root)
    provider = _FakeEmbeddingProvider(fail=False)
    _patch_provider(monkeypatch, provider)

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    coordinator = IndexCoordinator(ws_root)
    op_ctx = _make_op_ctx(ws_root)
    coordinator.register_builder(
        KNOWLEDGE_REPO_DOC_SOURCE_ID, _repo_build_fn("doc", op_ctx, "standard"), kind="static",
    )
    _run(coordinator.ensure_built(KNOWLEDGE_REPO_DOC_SOURCE_ID, await_completion=True))

    manifest = get_source_manifest(ws_root)
    before = _run(manifest.get(KNOWLEDGE_REPO_DOC_SOURCE_ID))
    assert before is not None and before.chunk_count == 3

    # Change README.md's content, remove docs/y.ja.md.
    (repo_root / "README.md").write_text("# Hello\nrepo readme body v2 CHANGED\n", encoding="utf-8")
    (repo_root / "docs" / "y.ja.md").unlink()

    _run(coordinator.mark_dirty(KNOWLEDGE_REPO_DOC_SOURCE_ID, reason="repo_change"))
    _run(coordinator.ensure_built(KNOWLEDGE_REPO_DOC_SOURCE_ID, await_completion=True))

    after = _run(manifest.get(KNOWLEDGE_REPO_DOC_SOURCE_ID))
    assert after is not None and after.chunk_count == 2, "removed file must not survive the reconcile build"

    backend = SqliteIndexBackend(workspace_root=ws_root)
    remaining_hashes = _run(backend.existing_hashes(KNOWLEDGE_REPO_DOC_SOURCE_ID))
    assert repo_content_hash("doc", "docs/y.ja.md") not in remaining_hashes
    assert repo_content_hash("doc", "README.md") in remaining_hashes

    # Verify the CHANGED text actually landed (not the stale v1 body).
    import sqlite3

    from reyn.data.index.backend import cache_dir_for_source
    db_file = cache_dir_for_source(ws_root, KNOWLEDGE_REPO_DOC_SOURCE_ID) / "index.db"
    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute(
            "SELECT text FROM chunks WHERE content_hash = ?",
            (repo_content_hash("doc", "README.md"),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and "v2 CHANGED" in row[0]


# ── 4. §G3 source-unit de-index via index_drop ───────────────────────────


def test_index_drop_deindexes_the_whole_repo_source_leaving_the_other_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: the firm's §4 ruling — repo de-index is SOURCE-UNIT, not
    per-file. The existing `index_drop` op (whole-source drop, live per
    FP-0066 P1b) applied to `knowledge_repo_doc` removes every entity that
    source holds; `knowledge_repo_src` (a DIFFERENT source) is untouched —
    proving the drop is scoped to the dropped source, not global."""
    repo_root = tmp_path / "fake_repo"
    _make_fake_repo(repo_root)
    _patch_repo_root(monkeypatch, repo_root)
    provider = _FakeEmbeddingProvider(fail=False)
    _patch_provider(monkeypatch, provider)

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    coordinator = IndexCoordinator(ws_root)
    op_ctx = _make_op_ctx(ws_root)
    coordinator.register_builder(
        KNOWLEDGE_REPO_DOC_SOURCE_ID, _repo_build_fn("doc", op_ctx, "standard"), kind="static",
    )
    coordinator.register_builder(
        KNOWLEDGE_REPO_SRC_SOURCE_ID, _repo_build_fn("src", op_ctx, "standard"), kind="static",
    )
    _run(coordinator.ensure_built(KNOWLEDGE_REPO_DOC_SOURCE_ID, await_completion=True))
    _run(coordinator.ensure_built(KNOWLEDGE_REPO_SRC_SOURCE_ID, await_completion=True))

    backend = SqliteIndexBackend(workspace_root=ws_root)
    assert len(_run(backend.existing_hashes(KNOWLEDGE_REPO_DOC_SOURCE_ID))) == 3
    assert len(_run(backend.existing_hashes(KNOWLEDGE_REPO_SRC_SOURCE_ID))) == 1

    drop_ctx = OpContext(
        workspace=Workspace(base_dir=ws_root, events=EventLog()), events=EventLog(),
        permission_decl=PermissionDecl(), permission_resolver=None,
    )
    result = _run(execute_op(
        IndexDropIROp(kind="index_drop", source=KNOWLEDGE_REPO_DOC_SOURCE_ID), drop_ctx,
    ))
    assert result["removed"] is True
    assert result["chunks_dropped"] == 3

    manifest = get_source_manifest(ws_root)
    assert _run(manifest.get(KNOWLEDGE_REPO_DOC_SOURCE_ID)) is None, (
        "the manifest entry for the dropped source must be gone"
    )
    remaining_doc_hashes = _run(backend.existing_hashes(KNOWLEDGE_REPO_DOC_SOURCE_ID))
    assert remaining_doc_hashes == set(), "no stale row may survive a whole-source drop"

    # The OTHER source is untouched — source-unit, not global.
    src_entry = _run(manifest.get(KNOWLEDGE_REPO_SRC_SOURCE_ID))
    assert src_entry is not None and src_entry.state == "clean"
    assert len(_run(backend.existing_hashes(KNOWLEDGE_REPO_SRC_SOURCE_ID))) == 1


# ── 5. recovery: dirty -> heal ────────────────────────────────────────────


def test_repo_build_failure_leaves_dirty_and_a_later_search_await_heals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: a background build fault (provider failure) leaves
    knowledge_repo_doc dirty; a later `search_await` with a WORKING
    provider heals it to clean — P2a's recovery path (same shape as
    P3a's real-producer memory/skill test), exercised for the repo
    producer for the first time."""
    repo_root = tmp_path / "fake_repo"
    _make_fake_repo(repo_root)
    _patch_repo_root(monkeypatch, repo_root)

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    coordinator = IndexCoordinator(ws_root)
    op_ctx = _make_op_ctx(ws_root)

    failing_provider = _FakeEmbeddingProvider(fail=True)
    _patch_provider(monkeypatch, failing_provider)
    coordinator.register_builder(
        KNOWLEDGE_REPO_DOC_SOURCE_ID, _repo_build_fn("doc", op_ctx, "standard"), kind="static",
    )
    outcome = _run(coordinator.ensure_built(KNOWLEDGE_REPO_DOC_SOURCE_ID, await_completion=True))
    assert outcome.error is not None

    manifest = get_source_manifest(ws_root)
    entry = _run(manifest.get(KNOWLEDGE_REPO_DOC_SOURCE_ID))
    assert entry is not None and entry.state in ("dirty", "error")

    working_provider = _FakeEmbeddingProvider(fail=False)
    _patch_provider(monkeypatch, working_provider)
    _run(coordinator.search_await(KNOWLEDGE_REPO_DOC_SOURCE_ID))

    healed = _run(manifest.get(KNOWLEDGE_REPO_DOC_SOURCE_ID))
    assert healed is not None and healed.state == "clean"
    assert healed.chunk_count == 3


# ── 6. #4431 — oversize-file skip visibility ─────────────────────────────


def test_oversize_repo_file_is_skipped_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: #4431 — a repo file over `_REPO_INGEST_MAX_BYTES` was
    silently dropped from the ingest corpus with no trail at all. It must
    now (a) still be excluded from the built chunks (unchanged behaviour —
    an oversize file has never been ingested) and (b) emit a
    `repo_ingest_files_skipped` audit-event carrying the count, so the gap
    has a witness instead of reading as "this file was never written"."""
    from reyn.data.index.knowledge_ingest import _REPO_INGEST_MAX_BYTES

    repo_root = tmp_path / "fake_repo"
    _make_fake_repo(repo_root)
    (repo_root / "docs" / "huge.md").write_text(
        "x" * (_REPO_INGEST_MAX_BYTES + 1), encoding="utf-8",
    )
    _patch_repo_root(monkeypatch, repo_root)
    provider = _FakeEmbeddingProvider(fail=False)
    _patch_provider(monkeypatch, provider)

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    coordinator = IndexCoordinator(ws_root)
    events = EventLog()
    collected = collect_events(events)
    ws = _make_op_ctx(ws_root).workspace
    op_ctx = OpContext(
        workspace=ws, events=events,
        permission_decl=PermissionDecl(),
    )
    coordinator.register_builder(
        KNOWLEDGE_REPO_DOC_SOURCE_ID, _repo_build_fn("doc", op_ctx, "standard"), kind="static",
    )
    _run(coordinator.ensure_built(KNOWLEDGE_REPO_DOC_SOURCE_ID, await_completion=True))

    manifest = get_source_manifest(ws_root)
    entry = _run(manifest.get(KNOWLEDGE_REPO_DOC_SOURCE_ID))
    # README.md + docs/x.md + docs/y.ja.md = 3 — the oversize file is NOT
    # among them (unchanged behaviour; only its visibility is new).
    assert entry is not None and entry.chunk_count == 3

    # Exactly one AGGREGATE event, not one per skipped file — the count is
    # in the payload, not in how many events fired. Unpacking a 1-item
    # generator fails the same way (ValueError) if that ever drifted,
    # without a bare length-equality assertion.
    (skip_event,) = (e for e in collected if e.type == "repo_ingest_files_skipped")
    assert skip_event.data["kind"] == "doc"
    assert skip_event.data["skipped_count"] == 1


def test_no_skip_event_when_nothing_is_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: accept-side twin — a build with no oversize files emits NO
    `repo_ingest_files_skipped` event at all (not one with count=0); the
    event names an actual gap, not a routine zero-report on every build."""
    repo_root = tmp_path / "fake_repo"
    _make_fake_repo(repo_root)
    _patch_repo_root(monkeypatch, repo_root)
    provider = _FakeEmbeddingProvider(fail=False)
    _patch_provider(monkeypatch, provider)

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    coordinator = IndexCoordinator(ws_root)
    events = EventLog()
    collected = collect_events(events)
    ws = _make_op_ctx(ws_root).workspace
    op_ctx = OpContext(
        workspace=ws, events=events,
        permission_decl=PermissionDecl(),
    )
    coordinator.register_builder(
        KNOWLEDGE_REPO_DOC_SOURCE_ID, _repo_build_fn("doc", op_ctx, "standard"), kind="static",
    )
    _run(coordinator.ensure_built(KNOWLEDGE_REPO_DOC_SOURCE_ID, await_completion=True))

    skip_events = [e for e in collected if e.type == "repo_ingest_files_skipped"]
    assert skip_events == []
