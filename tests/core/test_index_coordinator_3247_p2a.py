"""FP-0066 P2a (#3247) — IndexCoordinator core + SourceManifest dirty state.

Covers: mark_dirty state transition, ensure_built (await vs background),
search_await (steady-state no-op vs cold-start/dirty heal), is_ready, the
unified all-or-nothing embed-verify-write (+ its mandatory strip-falsify),
and the mandatory truncate-falsify recovery test (CLAUDE.md recovery-feature
gate) proving the dirty flag survives independently of the WAL.

No mocks — real ``SourceManifest``, real ``SqliteIndexBackend``, real
``OpContext``; a plain ``FakeEmbeddingProvider`` (same pattern as
``tests/core/test_action_embedding_index.py``) stands in for the litellm
boundary via the established ``get_provider`` monkeypatch convention.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.index import SqliteIndexBackend
from reyn.data.index.backend import ChunkRecord
from reyn.data.index.coordinator import (
    BuildMaterial,
    IndexCoordinator,
    assert_vector_count_match,
    embed_verify_write,
)
from reyn.data.index.source_manifest import SourceManifest, get_source_manifest
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionDecl
from tests._support.events import settle


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _run_and_settle(coro: Any, log: Any) -> Any:
    result = await coro
    await settle(log)
    return result


def _ctx_for(provider: Any, monkeypatch: pytest.MonkeyPatch) -> OpContext:
    """Real OpContext whose `embed` op resolves to ``provider`` (mirrors
    ``tests/core/test_action_embedding_index.py::_ctx_for``)."""
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(workspace=ws, events=events, permission_decl=PermissionDecl())


class _FakeEmbeddingProvider:
    """Deterministic canned vectors, one per input text (no litellm call)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.calls.append(tuple(texts))
        vectors = [[float((hash((t, i)) % 1000) / 1000.0) for i in range(4)] for t in texts]
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


class _DegenerateFakeProvider:
    """Always returns exactly 1 vector, regardless of input size — the
    partial-build repro used by the all-or-nothing strip-falsify test."""

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        return {"vectors": [[1.0, 0.0]], "model": model, "total_tokens": 1}


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
        score=None,
    )


def _working_build_fn(ctx: OpContext, items: list[dict]):
    async def _build() -> BuildMaterial:
        return BuildMaterial(
            items=items,
            texts=[it["text"] for it in items],
            to_chunk_record=_to_chunk_record,
            model_class="standard",
            ctx=ctx,
        )
    return _build


# ── 1. mark_dirty — state transition ────────────────────────────────────────


def test_mark_dirty_persists_dirty_state_and_reason(tmp_path: Path) -> None:
    """Tier 2: mark_dirty sets state="dirty" + last_error on the manifest
    entry, persisted to sources.yaml (no entry needs to pre-exist)."""
    coord = IndexCoordinator(tmp_path)
    _run(coord.mark_dirty("skill", reason="provider_error: timeout"))

    manifest = get_source_manifest(tmp_path)
    entry = _run(manifest.get("skill"))
    assert entry is not None
    assert entry.state == "dirty"
    assert entry.last_error == "provider_error: timeout"


def test_mark_dirty_on_existing_clean_entry_flips_to_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: a previously-clean (built) source can be marked dirty without
    losing its chunk_count (only state + last_error change)."""
    coord = IndexCoordinator(tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    items = [{"id": "a", "text": "alpha"}]
    coord.register_builder("mem", _working_build_fn(ctx, items), kind="dynamic")
    _run(coord.ensure_built("mem", await_completion=True))

    manifest = get_source_manifest(tmp_path)
    before = _run(manifest.get("mem"))
    assert before is not None and before.state == "clean" and before.chunk_count == 1

    _run(coord.mark_dirty("mem", reason="remember_failed"))
    after = _run(manifest.get("mem"))
    assert after is not None
    assert after.state == "dirty"
    assert after.last_error == "remember_failed"
    assert after.chunk_count == 1, "chunk_count from the prior build must survive the dirty mark"


# ── 2. ensure_built — await vs background, clean no-op ────────────────────


def test_ensure_built_await_completion_builds_synchronously(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 2: ensure_built(await_completion=True) runs the build in-line and
    returns a BuildOutcome with the written chunk_count; the manifest state
    transitions to clean."""
    coord = IndexCoordinator(tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    items = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]
    coord.register_builder("skill", _working_build_fn(ctx, items), kind="dynamic")

    outcome = _run(coord.ensure_built("skill", await_completion=True))

    assert outcome.triggered is True
    assert outcome.background is False
    assert outcome.chunk_count == 2
    assert outcome.error is None
    assert _run(coord.is_ready("skill")) is True


def test_ensure_built_background_schedules_a_task_and_completes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 2: ensure_built(await_completion=False) returns immediately with
    background=True; awaiting the loop lets the scheduled task finish and
    the manifest observably transitions to clean."""
    coord = IndexCoordinator(tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    items = [{"id": "a", "text": "alpha"}]
    coord.register_builder("repo_doc", _working_build_fn(ctx, items), kind="dynamic")

    async def _scenario() -> None:
        outcome = await coord.ensure_built("repo_doc", await_completion=False)
        assert outcome.background is True
        assert outcome.triggered is True
        # Poll the PUBLIC readiness gate for the scheduled background task
        # to complete (no private-state introspection). #3748: unbounded
        # (owner policy) -- no terminating assert: the loop condition IS
        # that check.
        while not await coord.is_ready("repo_doc"):
            await asyncio.sleep(0.01)

    _run(_scenario())


def test_ensure_built_clean_source_is_a_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 2: a source already state==clean short-circuits without
    re-invoking the builder (no re-embed)."""
    coord = IndexCoordinator(tmp_path)
    provider = _FakeEmbeddingProvider()
    ctx = _ctx_for(provider, monkeypatch)
    items = [{"id": "a", "text": "alpha"}]
    coord.register_builder("skill", _working_build_fn(ctx, items), kind="dynamic")
    _run(coord.ensure_built("skill", await_completion=True))
    calls_after_first_build = len(provider.calls)

    outcome = _run(coord.ensure_built("skill", await_completion=True))

    assert outcome.triggered is False
    assert len(provider.calls) == calls_after_first_build, "clean source must not re-embed"


# ── 3. search_await — steady-state no-op vs dirty heal ─────────────────────


def test_search_await_on_clean_source_is_a_cheap_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 2: search_await on a clean source does not invoke the builder
    (steady-state = cheap state-check only, per the firm's §5 contract)."""
    coord = IndexCoordinator(tmp_path)
    provider = _FakeEmbeddingProvider()
    ctx = _ctx_for(provider, monkeypatch)
    items = [{"id": "a", "text": "alpha"}]
    coord.register_builder("skill", _working_build_fn(ctx, items), kind="dynamic")
    _run(coord.ensure_built("skill", await_completion=True))
    calls_after_build = len(provider.calls)

    _run(coord.search_await("skill"))

    assert len(provider.calls) == calls_after_build, "clean-state search_await must not build"


def test_search_await_heals_a_dirty_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 2: search_await on a dirty source triggers a synchronous rebuild
    (the §G2 completeness guarantee: a prior best-effort failure's dirty
    mark gets healed at the next search)."""
    coord = IndexCoordinator(tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    items = [{"id": "a", "text": "alpha"}]
    coord.register_builder("memory", _working_build_fn(ctx, items), kind="dynamic")
    _run(coord.mark_dirty("memory", reason="provider_error"))
    assert _run(coord.is_ready("memory")) is False

    _run(coord.search_await("memory"))

    assert _run(coord.is_ready("memory")) is True


def test_search_await_missing_source_is_a_noop(tmp_path: Path) -> None:
    """Tier 2: search_await on a never-registered/never-built source_id is a
    silent no-op (nothing to await)."""
    coord = IndexCoordinator(tmp_path)
    _run(coord.search_await("nonexistent"))  # must not raise


# ── 4. is_ready ──────────────────────────────────────────────────────────


def test_is_ready_false_before_build_true_after(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 2: is_ready reflects the persisted state==clean gate."""
    coord = IndexCoordinator(tmp_path)
    assert _run(coord.is_ready("skill")) is False
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    coord.register_builder("skill", _working_build_fn(ctx, [{"id": "a", "text": "a"}]), kind="dynamic")
    _run(coord.ensure_built("skill", await_completion=True))
    assert _run(coord.is_ready("skill")) is True


# ── 5. all-or-nothing unification — positive + strip-falsify ──────────────


def test_assert_vector_count_match_raises_on_mismatch() -> None:
    """Tier 2: the unified guard raises RuntimeError naming the item_noun/
    label (both existing call sites' distinct wording is preserved via
    these params)."""
    with pytest.raises(RuntimeError, match="refusing partial build"):
        assert_vector_count_match(1, 2, item_noun="items", label="build")
    with pytest.raises(RuntimeError, match="refusing partial index_update write"):
        assert_vector_count_match(1, 2, item_noun="chunks", label="index_update write")


def test_embed_verify_write_refuses_partial_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 2: embed_verify_write, with the REAL (unpatched) guard, raises
    before writing anything when the provider returns too few vectors — no
    partial batch reaches the backend."""
    ctx = _ctx_for(_DegenerateFakeProvider(), monkeypatch)
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    items = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]

    with pytest.raises(RuntimeError, match="refusing partial build"):
        _run(embed_verify_write(
            ctx=ctx, texts=[it["text"] for it in items], model_class="standard",
            items=items, to_chunk_record=_to_chunk_record, backend=backend,
            source="strip_test", mode="replace", item_noun="items", label="build",
        ))

    stat = _run(backend.stat("strip_test"))
    assert stat["chunk_count"] == 0, "no partial write must have reached the backend"


def test_embed_verify_write_carries_the_embed_ops_own_cost_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: #4157 — EmbedWriteResult carries total_tokens/cost_usd from
    the embed op's OWN result (EmbedBatchResult.total_tokens +
    estimate_embedding_cost), not re-measured. "standard" isn't a real
    litellm-priced model name, so cost_usd correctly comes back None
    (unpriced != free, #1829) while total_tokens is still the real count —
    proving the two are threaded independently, not one derived from the
    other's presence."""
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    items = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]

    result = _run(embed_verify_write(
        ctx=ctx, texts=[it["text"] for it in items], model_class="standard",
        items=items, to_chunk_record=_to_chunk_record, backend=backend,
        source="cost_test", mode="replace", item_noun="items", label="build",
    ))

    assert result.total_tokens == 2, "one token per item, from the fake provider"
    assert result.cost_usd is None, "unpriced model — None, not fabricated 0.0"


def test_build_complete_event_carries_cost_fields_not_just_chunk_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: #4157 — embedding_index_build_complete's audit-event payload
    reports total_tokens/cost_usd/embedding_model, not chunk_count alone.
    Before this fix the event carried only chunk_count (1609 in the
    owner-witnessed live report) — the same values EmbedWriteResult now
    carries are asserted on the actual EMITTED event, not just the return
    value, since the event is what the audit trail actually persists."""
    from tests._support.events import collect_events

    events = EventLog()
    collected = collect_events(events)
    coord = IndexCoordinator(tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    items = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]
    coord.register_builder("skill", _working_build_fn(ctx, items), kind="dynamic")

    _run(_run_and_settle(
        coord.ensure_built("skill", await_completion=True, events=events), events,
    ))

    (complete,) = [e for e in collected if e.type == "embedding_index_build_complete"]
    assert complete.data["chunk_count"] == 2
    assert complete.data["total_tokens"] == 2, (
        f"the event must carry total_tokens, not just chunk_count: {complete.data}"
    )
    assert complete.data["embedding_model"] == "standard"
    assert "cost_usd" in complete.data


def test_strip_falsify_neutered_guard_accepts_partial_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 2: (strip-falsify, #3247 architect-required) neutering the
    unified all-or-nothing guard (monkeypatch it to a no-op) causes the
    SAME mismatched-vector-count input that the previous test proved gets
    REJECTED to instead be silently ACCEPTED as a partial write (1 chunk
    persisted for 2 requested items, no exception). This demonstrates the
    guard itself — not incidental plumbing around it — is what prevents
    the partial-write data-loss vector. RED (of the guarantee) is exactly
    this test passing."""
    import reyn.data.index.coordinator as coordinator_mod

    monkeypatch.setattr(coordinator_mod, "assert_vector_count_match", lambda *a, **kw: None)
    ctx = _ctx_for(_DegenerateFakeProvider(), monkeypatch)
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    items = [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}]

    result = _run(embed_verify_write(
        ctx=ctx, texts=[it["text"] for it in items], model_class="standard",
        items=items, to_chunk_record=_to_chunk_record, backend=backend,
        source="strip_test", mode="replace", item_noun="items", label="build",
    ))

    stat = _run(backend.stat("strip_test"))
    assert stat["chunk_count"] == 1, (
        "with the guard neutered, zip(items, vectors) silently truncates to "
        "the SHORTER list -- a 1-vector response for 2 requested items writes "
        "exactly 1 chunk instead of raising; this is the partial-write bug "
        "the real (unpatched) guard exists to prevent"
    )
    assert result.write_result["written"] == 1


# ── 6. truncate-falsify — recovery-feature gate (CLAUDE.md, #2259/#2260-class) ─


def test_dirty_flag_survives_wal_truncation_and_search_await_heals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: (truncate-falsify, mandatory recovery-feature gate) the
    IndexCoordinator's dirty state is recorded in sources.yaml -- a file
    completely independent of the agent-state WAL
    (``.reyn/state/wal.jsonl``, see docs/concepts/runtime/events.md "WAL vs
    audit-event separation"). This test proves recovery-of-record is the
    persisted sources.yaml dirty flag, NOT anything WAL-derived, AND that
    the "restart" genuinely reloads from the FILE rather than riding an
    in-memory manifest cache still alive in the same process:

      1. Build a source to state==clean (coordinator instance #1).
      2. mark_dirty (simulating a sync-in-op provider failure, §G2).
      3. Write SOME WAL entries, then TRUNCATE the WAL file to empty --
         simulating a crash where the WAL substrate loses everything past
         (and including) the point the dirty mark was set. If the dirty
         flag depended on the WAL, it would be gone here.
      4. Reset the per-workspace ``SourceManifest`` SINGLETON cache
         (``get_source_manifest`` caches one instance per resolved
         workspace_root in a module-level dict) -- WITHOUT this, step 5
         below would silently pass by reading the SAME in-memory
         ``SourceEntry`` object rather than genuinely re-parsing
         ``sources.yaml``, which would make the test vacuous with respect
         to file-persistence (see the paired strip-falsify test below,
         which proves this reset is what makes the gate non-vacuous).
      5. Discard coordinator #1 entirely (its in-memory build-queue /
         failure-memo are volatile BY DESIGN -- simulates a process
         restart) and construct a FRESH coordinator #2 against the SAME
         workspace_root -- it gets a brand-new ``SourceManifest`` that has
         never read anything, forcing a real file parse on first access.
      6. Assert the dirty state SURVIVED (read purely from a freshly
         re-parsed sources.yaml) and that a fresh search_await (with a
         newly-registered builder, as a real restarted process would
         re-register its domain adapters) HEALS it -- re-running the
         build and returning to state==clean.
    """
    from reyn.core.events.state_log import StateLog

    coord1 = IndexCoordinator(tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    items = [{"id": "a", "text": "alpha"}]
    coord1.register_builder("doc_source", _working_build_fn(ctx, items), kind="dynamic")
    _run(coord1.ensure_built("doc_source", await_completion=True))
    assert _run(coord1.is_ready("doc_source")) is True

    _run(coord1.mark_dirty("doc_source", reason="provider_error: transient timeout"))

    # Simulate WAL activity around the dirty mark, then truncate it away --
    # proving the dirty flag's survival has nothing to do with WAL content.
    wal_path = tmp_path / ".reyn" / "state" / "wal.jsonl"
    wal_path.parent.mkdir(parents=True, exist_ok=True)
    log = StateLog(wal_path)
    _run(log.append("inbox_put", target="some-agent", msg_id="m1", msg_kind="user", payload={}))
    _run(log.flush())
    assert wal_path.exists() and wal_path.stat().st_size > 0
    wal_path.write_bytes(b"")  # truncate below (= including) the dirty-mark point

    # "restart": drop coordinator #1's in-memory state AND the per-workspace
    # SourceManifest singleton cache (else coord2 below would transparently
    # ride #1's already-loaded in-memory SourceEntry, never touching disk --
    # see test_reload_after_singleton_reset_depends_on_real_file_persist for
    # the strip-falsify proof that this reset is what forces a real reload).
    del coord1
    _reset_manifest_singleton(tmp_path)
    coord2 = IndexCoordinator(tmp_path)

    entry_after_restart = _run(get_source_manifest(tmp_path).get("doc_source"))
    assert entry_after_restart is not None
    assert entry_after_restart.state == "dirty", (
        "the dirty flag must survive a fully-truncated WAL AND a fresh "
        "SourceManifest re-parse of sources.yaml -- it is persisted on "
        "disk, independent of both the WAL substrate and any in-process "
        "manifest cache"
    )
    assert _run(coord2.is_ready("doc_source")) is False

    # A restarted process re-registers its domain builder, then a search
    # triggers the heal (§G2's recovery path).
    coord2.register_builder("doc_source", _working_build_fn(ctx, items), kind="dynamic")
    _run(coord2.search_await("doc_source"))

    assert _run(coord2.is_ready("doc_source")) is True
    healed_entry = _run(get_source_manifest(tmp_path).get("doc_source"))
    assert healed_entry is not None
    assert healed_entry.state == "clean"
    assert healed_entry.last_error is None


def _reset_manifest_singleton(workspace_root: Path) -> None:
    """Evict the ``get_source_manifest`` per-workspace singleton cache so the
    next call constructs a fresh ``SourceManifest`` (empty in-memory cache,
    forced to re-parse ``sources.yaml`` from disk on first read) -- the
    same-process equivalent of a process restart for THIS specific cache."""
    import reyn.data.index.source_manifest as _sm_mod

    _sm_mod._MANIFESTS.pop(workspace_root.resolve(), None)


def test_reload_after_singleton_reset_depends_on_real_file_persist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tier 2: (strip-falsify, #3247 co-vet) proves the singleton reset in
    the truncate-falsify test above is not itself vacuous -- it genuinely
    forces a read FROM ``sources.yaml``, not from an in-memory cache.

    Neuters ``SourceManifest._atomic_write`` (the real on-disk persist) to a
    no-op so ``mark_dirty``'s ``upsert`` updates ONLY the in-memory cache,
    never the file. Confirms the (expected, uninteresting) in-process read
    still shows "dirty" via the live cache. Then resets the singleton
    (forcing a fresh ``SourceManifest`` that has never cached anything) and
    re-reads: because the file was never actually written, the entry comes
    back "clean" (or absent) -- NOT "dirty". This is the falsifying case:
    if the truncate-falsify test's post-restart assertion
    (``state == "dirty"``) were run against a build whose file-persistence
    is broken, it would FAIL here -- proving that test's green result
    requires genuine file persistence, not merely the singleton surviving
    in memory.
    """
    coord = IndexCoordinator(tmp_path)
    ctx = _ctx_for(_FakeEmbeddingProvider(), monkeypatch)
    items = [{"id": "a", "text": "alpha"}]
    coord.register_builder("doc_source", _working_build_fn(ctx, items), kind="dynamic")
    _run(coord.ensure_built("doc_source", await_completion=True))
    assert _run(coord.is_ready("doc_source")) is True

    # Neuter the real on-disk persist -- upsert() still updates the
    # in-memory cache, but the write to sources.yaml never happens.
    async def _noop_atomic_write(self, *, sandbox_write_paths=None) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(SourceManifest, "_atomic_write", _noop_atomic_write)

    _run(coord.mark_dirty("doc_source", reason="provider_error: transient timeout"))

    # Same-process, same singleton: the in-memory cache DOES show dirty --
    # this is the uninteresting case the original (un-fixed) test relied on.
    same_process_entry = _run(get_source_manifest(tmp_path).get("doc_source"))
    assert same_process_entry is not None and same_process_entry.state == "dirty", (
        "sanity check: the in-memory cache reflects the mark_dirty call "
        "regardless of whether the file write happened"
    )

    # Reset the singleton -- the next get_source_manifest() constructs a
    # FRESH SourceManifest with an empty cache, forced to parse the file.
    _reset_manifest_singleton(tmp_path)
    reloaded_entry = _run(get_source_manifest(tmp_path).get("doc_source"))

    assert reloaded_entry is not None
    assert reloaded_entry.state != "dirty", (
        "with the real file-write neutered, a genuine reload from "
        "sources.yaml must NOT observe 'dirty' -- it was never durably "
        "written. Seeing 'dirty' here would mean the singleton reset "
        "failed to force a real file re-parse, which would make the "
        "truncate-falsify test's post-restart assertion vacuous."
    )
