"""IndexCoordinator — orchestration for embedding-index builds (FP-0066 P2a,
#3247 "P2 IndexCoordinator 設計 firm").

**Boundary principle (the firm's one-line frame)**: the Coordinator owns
*orchestration* — dirty-marking, await, the background build queue, the
cross-process ``build_lock``, failure-memoization, and the readiness gate.
It never owns *execution* (the ``embed``/``index_update`` ops + the
``SqliteIndexBackend`` remain the real index write — layer-3, kept from
FP-0066 P1) and never owns *domain policy* (a source's own item↔ChunkRecord
mapping and what to embed is supplied by the caller as a ``BuildFn`` "domain
adapter" strategy callback — see ``register_builder`` below). Centralising
the await/build-queue orchestration here is what prevents the "file.read
dispersion" replay the firm calls out: without one owner, await logic would
scatter across install/remember/search call sites again.

**P2a scope** (per the firm's §7 sub-PR decomposition): the Coordinator
CORE + the ``SourceManifest`` dirty/pending state, plus the ONE unified
all-or-nothing embed-verify-write primitive (``embed_verify_write``) that
``ActionEmbeddingIndex.build()`` and the ``index_update`` op used to
duplicate verbatim. Migrating those two call sites to trigger builds
*through* the Coordinator (eager-vs-background, once-per-chain spawn) is
**P2b** — done (#3260). The architect's decomposition-correction comment on
#3247 folds the original P2c (sync-in-op op wiring + G3 delete-de-index)
INTO P3 — its only producers (skill/memory knowledge ingest, doc-RAG
``index_update``) are all P3-dependent (0 live callers today), so building
that wiring here would be a producer-less framework. **P2d (this module's
current state)**: audit-event phase emission (``embedding_index_build_
started``/``_progress``/``_complete``/``_error`` — the last folding the
pre-P2d ``action_index_build_failed`` event, which used to be emitted
directly by ``RouterLoop._build_action_embedding_index_background`` — see
``ensure_built``'s ``events`` parameter)
plus the ``search_await`` contract's production wiring at the two live
action-catalog query call sites (``RouterLoop.search_actions`` /
``universal_catalog._handle_search_actions``).

**P2-convergence PR1** (#3270 §2, design firm on #3270): collapses the P2b
two-path Coordinator API (``ensure_built`` for material-producing adapters
vs ``ensure_built_self_contained`` for adapters that owned their own
lock+write, e.g. the action-catalog) down to the single ``ensure_built`` —
``ensure_built_self_contained`` is REMOVED. ``ActionEmbeddingIndex.build()``
lost both its own locks (the in-process ``asyncio.Lock`` and the
cross-process ``try_acquire_build_lock``); the Coordinator's own lock
acquisition in ``_run_build`` (below) is now the SOLE holder for every
registered source, which makes the same-path double-acquire that used to
motivate the two-path split (self-deadlock-shaped: the second
``try_acquire_build_lock`` call sees ITS OWN pid as a live holder and
silently no-ops) structurally impossible rather than merely avoided. The
action-catalog's disk-adopt/dual-axis-invalidation POLICY stays a domain
concern — extracted into ``ActionEmbeddingIndex.prepare_material``, a
``BuildFn`` that returns ``BuildMaterial`` (real rebuild needed) or
``None`` (the adapter's own policy determined no write is needed this
call — see ``_run_build``'s ``material is None`` branch and ``BuildFn``'s
docstring).

**Crash-recovery (the band requirement, CLAUDE.md recovery-feature gate)**:
the dirty/building/error state lives in ``SourceEntry`` (persisted to
``sources.yaml`` — the SourceManifest's existing atomic-write file SSoT).
This is DELIBERATELY NOT the WAL (``.reyn/state/wal.jsonl``) — the WAL is
agent chat-state's crash-recovery substrate (see
``docs/concepts/runtime/events.md`` — "WAL vs audit-event separation"); the
IndexCoordinator's dirty flag has nothing to do with it and must survive
completely independently of it. The in-memory build-queue (``_bg_tasks``,
``_failure_memo``) is volatile BY DESIGN — a crash loses it, and that is
fine, because ``search_await`` re-derives "does this need a build" from the
persisted ``sources.yaml`` state alone, not from the in-memory queue. See
``tests/test_index_coordinator_3247_p2a.py`` for the truncate-falsify proof.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from reyn.data.index import ChunkRecord, IndexBackend, WriteResult, get_backend
from reyn.data.index.backend import cache_dir_for_source
from reyn.data.index.build_lock import try_acquire_build_lock
from reyn.data.index.source_manifest import (
    SourceEntry,
    SourceKind,
    SourceManifest,
    get_source_manifest,
)

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext

__all__ = [
    "BuildMaterial",
    "BuildFn",
    "BuildOutcome",
    "EmbedWriteResult",
    "assert_vector_count_match",
    "embed_verify_write",
    "emit_wrapped_semantic_search",
    "IndexCoordinator",
    "get_index_coordinator",
]


# ── Unified all-or-nothing embed-verify-write (the firm's dedup target) ────


@dataclass(frozen=True)
class EmbedWriteResult:
    """Return shape of ``embed_verify_write`` — the write outcome plus the
    embed op's resolved model id (some callers, e.g. ``index_update``, must
    record the resolved model on subsequent chunk metadata / the manifest)."""

    write_result: WriteResult
    resolved_model: str


def assert_vector_count_match(
    vector_count: int, item_count: int, *, item_noun: str = "items", label: str = "build",
) -> None:
    """The all-or-nothing guard, itself — the ONE canonical implementation of
    what used to be a verbatim-duplicated ``if len(vectors) != len(items):
    raise RuntimeError(...)`` in both ``ActionEmbeddingIndex.build()`` and
    the ``index_update`` op. Both call sites now call THIS function for
    their count-match check (``item_noun``/``label`` preserve each site's
    distinct error-message wording — "...refusing partial build" vs
    "...refusing partial index_update write" — respectively; behavior is
    now identical by construction, not by two hand-maintained copies
    agreeing).

    Public (not underscore-prefixed) precisely so it can be imported and
    monkeypatched-to-a-no-op by the strip-falsify test that proves this
    guard — not just the surrounding plumbing — is load-bearing: neuter
    it and a mismatched vector count silently writes a partial batch
    instead of raising.
    """
    if vector_count != item_count:
        raise RuntimeError(
            f"embed returned {vector_count} vectors for {item_count} "
            f"{item_noun}; refusing partial {label}"
        )


async def embed_verify_write(
    *,
    ctx: "OpContext",
    texts: list[str],
    model_class: str,
    items: list[Any],
    to_chunk_record: Callable[[Any, list[float], str], ChunkRecord],
    backend: IndexBackend,
    source: str,
    mode: str = "replace",
    item_noun: str = "items",
    label: str = "build",
) -> EmbedWriteResult:
    """Embed ``texts`` via the shared ``embed`` op, verify the returned
    vector count matches ``len(items)`` (raising ``RuntimeError`` on a
    mismatch — the all-or-nothing guard), map each ``(item, vector)`` pair
    to a ``ChunkRecord`` via the caller-supplied ``to_chunk_record``, and
    write the batch to ``backend``.

    This is the ONE canonical implementation of the verification that used
    to exist twice, verbatim, in ``ActionEmbeddingIndex.build()`` (message:
    "...refusing partial build") and the ``index_update`` op (message:
    "...refusing partial index_update write") — both call sites now route
    through this function (``item_noun``/``label`` preserve their distinct
    error-message wording; the *behavior* — raise-before-write on a count
    mismatch — is now byte-identical by construction, not by two hand-
    maintained copies agreeing).
    """
    from reyn.core.op_runtime import execute_op
    from reyn.schemas.models import EmbedIROp

    result = await execute_op(
        EmbedIROp(kind="embed", texts=texts, embedding_model=model_class), ctx,
    )
    if result.get("status") == "error":
        raise RuntimeError(f"embed op failed: {result.get('error')}")
    vectors = list(result.get("vectors", []))
    assert_vector_count_match(
        len(vectors), len(items), item_noun=item_noun, label=label,
    )
    resolved_model = str(result.get("model", model_class))
    records = [
        to_chunk_record(item, vector, resolved_model)
        for item, vector in zip(items, vectors)
    ]
    write_result = await backend.write(source, records, mode=mode)  # type: ignore[arg-type]
    return EmbedWriteResult(write_result=write_result, resolved_model=resolved_model)


# ── IndexCoordinator public interface (#3247 firm §1) ──────────────────────


@dataclass(frozen=True)
class BuildMaterial:
    """What a domain adapter (the ``BuildFn`` strategy callback) supplies so
    the Coordinator can run a build without knowing anything domain-specific.

    ``items``/``texts`` are parallel (``len(items) == len(texts)``);
    ``to_chunk_record(item, vector, resolved_model) -> ChunkRecord`` is the
    domain's item↔ChunkRecord mapping (kept OUT of the Coordinator per the
    firm's boundary principle — e.g. the action-catalog's dual-axis
    category/qualified_name mapping, or a doc-RAG source's chunk metadata).
    """

    items: list[Any]
    texts: list[str]
    to_chunk_record: Callable[[Any, list[float], str], ChunkRecord]
    model_class: str
    ctx: "OpContext"


BuildFn = Callable[[], Awaitable["BuildMaterial | None"]]
"""A domain adapter's build strategy. Returns ``BuildMaterial`` when a real
embed+write is needed (the Coordinator's ``_run_build`` then owns
``embed_verify_write``), or ``None`` when the adapter determined — as part
of its OWN material-generation policy (e.g. a disk-adopt cache hit) — that
no write is needed this call (P2-convergence PR1, #3270 §2: this is how the
now-eliminated ``ensure_built_self_contained`` two-path shape folds into the
single ``ensure_built``. See ``ActionEmbeddingIndex.prepare_material`` for
the one adapter that exercises the ``None`` branch today)."""


@dataclass(frozen=True)
class BuildOutcome:
    """Result of an ``ensure_built`` (or an internally-driven heal) call.

    ``triggered``: a build was attempted this call (vs. a cheap clean-state
    no-op). ``background``: the build was scheduled as a fire-and-forget
    ``asyncio.create_task`` rather than awaited in-line. ``chunk_count``:
    populated on a successful synchronous build. ``error``: populated
    (without raising) when the build failed — ``ensure_built`` never raises;
    a failure is best-effort-recorded (dirty + failure-memo) and reported
    via this field, per the firm's §G2 best-effort contract.
    """

    source_id: str
    triggered: bool
    background: bool
    chunk_count: int | None = None
    error: str | None = None


class IndexCoordinator:
    """Per-workspace orchestrator for embedding-index builds.

    Singleton-per-workspace is the caller's choice (mirrors
    ``SourceManifest``/``get_source_manifest``) — this class itself does not
    enforce singleton-ness; production wiring (P2b/P2c) will decide where
    one instance lives (likely session-scoped, one per workspace).

    A source must be registered via ``register_builder`` before
    ``ensure_built``/``search_await`` can build/heal it — this is necessary
    plumbing the firm's interface section does not spell out (its
    ``ensure_built(source_id, *, await_completion)`` signature carries no
    build strategy per call), so the strategy has to be associated with the
    ``source_id`` some other way. Registering a builder is how a domain
    adapter tells the Coordinator "this is how you'd build me" — P2b wired
    the first production registrants (memory/skill/repo, via
    ``knowledge_ingest.py``); P2-convergence PR1 (#3270 §2) adds the
    action-catalog (``RouterLoop._ensure_action_index_built``, routed
    through ``ActionEmbeddingIndex.prepare_material`` as its ``BuildFn``),
    eliminating the parallel ``ensure_built_self_contained`` entry point
    that previously carried it.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        backend: IndexBackend | None = None,
        manifest: SourceManifest | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._backend: IndexBackend = (
            backend if backend is not None else get_backend("sqlite", workspace_root=workspace_root)
        )
        self._manifest: SourceManifest = (
            manifest if manifest is not None else get_source_manifest(workspace_root)
        )
        self._builders: dict[str, BuildFn] = {}
        # Volatile by design (crash-recovery band): lost on process restart.
        # Recovery does NOT depend on these — see module docstring.
        self._bg_tasks: dict[str, "asyncio.Task[BuildOutcome]"] = {}
        self._failure_memo: dict[str, str] = {}
        # FP-0066 P2b (#3247 firm §2): per-source kind, remembered so a
        # freshly-created manifest entry (mark_dirty/_set_state on a
        # source never seen before) is tagged correctly instead of falling
        # through to the "backfill" dataclass default. Registered
        # alongside the build strategy (see ``register_builder``); a source
        # that never registers a kind stays "backfill" (predates the
        # taxonomy).
        self._kinds: dict[str, SourceKind] = {}

    # ── registration (necessary plumbing, not a P2b call-site migration) ──

    def register_builder(
        self, source_id: str, build_fn: BuildFn, *, kind: SourceKind,
    ) -> None:
        """Associate a domain build strategy with ``source_id``.

        Idempotent — re-registering replaces the prior strategy (useful for
        tests and for a future config-driven re-registration on reload).
        ``kind`` (FP-0066 P2b, #3247 firm §2) tags a freshly-created
        manifest entry for this source with its taxonomy classification;
        re-registering with a different ``kind`` updates the remembered
        value for the NEXT freshly-created entry (an already-persisted
        entry's ``kind`` is not silently overwritten by re-registration).

        ``kind`` is a REQUIRED keyword-only argument (FP-0066 P3a, #3247
        firm §7(b)) — it used to default to ``"backfill"``, silently
        misclassifying any new registration that forgot to pass one. A
        silent default hides exactly the failure mode the taxonomy exists
        to catch: a registration omission reading back as "this source
        predates the taxonomy" instead of "whoever registered this forgot
        to say what kind it is". Loud-by-construction (a missing ``kind``
        is now a ``TypeError`` at the call site, not a wrong-but-plausible
        default) — mirrors the fail-closed/loud-error precedent set by
        ``load_skill``'s unregistered-skill ``ValueError`` (#3256). The
        pre-taxonomy ``sources.yaml`` on-disk coercion
        (``SourceEntry.from_dict``'s ``_coerce_kind`` — a persisted entry
        with no/garbled ``kind`` field) is UNCHANGED and stays "backfill":
        that is a genuine migration default for data written before this
        taxonomy existed, not a silent fallback for new code.
        """
        self._builders[source_id] = build_fn
        self._kinds[source_id] = kind

    # ── mark_dirty (#3247 firm §1) ──────────────────────────────────────

    async def mark_dirty(self, source_id: str, *, reason: str) -> None:
        """Best-effort dirty mark — the §G2 provider-failure recovery hook.

        Persists ``state="dirty"`` + ``last_error=reason`` to
        ``sources.yaml`` via ``SourceManifest.upsert`` (the workspace-SSoT
        band: no second state store). If no entry exists yet for
        ``source_id`` (a build never even reached a first write), a minimal
        placeholder entry is created so the dirty mark is not silently lost
        — a later real build overwrites the placeholder fields.
        """
        entry = await self._manifest.get(source_id)
        if entry is None:
            entry = SourceEntry(
                name=source_id, description="", path="", backend="sqlite",
                kind=self._kinds.get(source_id, "backfill"),
            )
        entry.state = "dirty"
        entry.last_error = reason
        await self._manifest.upsert(entry)

    # ── ensure_built (#3247 firm §1) ─────────────────────────────────────

    async def ensure_built(
        self,
        source_id: str,
        *,
        await_completion: bool,
        events: "EventLog | None" = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_success: Callable[[BuildOutcome], None] | None = None,
    ) -> BuildOutcome:
        """If ``source_id`` is dirty/error/never-built, (re)build it.

        ``await_completion=True`` runs the build in-line and returns once
        done (sync-in-op await, per the firm's §3 dynamic-kind rule).
        ``await_completion=False`` schedules ``asyncio.create_task`` and
        returns immediately (background, per §3 static/backfill rule) —
        at most one background task per ``source_id`` is live at a time
        (once-per-source spawn, mirrors ``RouterLoop``'s
        ``_action_index_build_task`` once-per-chain dedup).

        Never raises: a build failure is caught, recorded via
        ``mark_dirty`` + the in-process failure-memo, and reported on the
        returned ``BuildOutcome.error`` (the §G2 best-effort contract).

        ``events`` (FP-0066 P2d, #3247 firm §6): an optional ``EventLog`` to
        emit the ``embedding_index_build_started``/``_progress``/
        ``_complete``/``_error`` audit-event phases to. ``None`` (the
        default — matches every other best-effort-optional collaborator on
        this class) silently skips the audit-emit; a real build call site
        (production or test) threads its ``EventLog`` here to get the P6
        audit trail.

        ``on_error`` (P2-convergence PR1, #3270 §2): an optional best-effort
        callback invoked with the real ``Exception`` instance on a build
        failure — BEFORE this method returns, regardless of
        ``await_completion`` (it runs from inside ``_run_build``, the same
        coroutine body whether awaited inline or scheduled as a background
        task, so it fires uniformly for both). This is the seam a caller
        with its OWN failure bookkeeping to keep in sync (e.g.
        ``RouterLoop._action_index_build_failed``, #1458) hooks into,
        without the Coordinator needing to know that bookkeeping exists —
        the callback receives the ORIGINAL exception object (not just its
        ``str()``), which a cause-aware caller (e.g.
        ``_action_index_build_failure_warning``'s exception-type branching)
        needs and ``BuildOutcome.error`` (a string) cannot carry.

        ``on_success`` (P2-convergence PR1, #3270 §2): the success-path
        mirror of ``on_error`` — an optional best-effort callback invoked
        with the ``BuildOutcome`` when a build (real write OR a
        material-generation ``None`` no-op) completes without raising,
        again from inside ``_run_build`` so it fires uniformly for both
        ``await_completion`` values. The seam a caller whose OWN
        in-memory state needs syncing after a Coordinator-driven write
        (e.g. ``ActionEmbeddingIndex.adopt_build_result``, since the
        Coordinator — not the domain adapter — now performs
        ``embed_verify_write`` for a material-producing ``BuildFn``) hooks
        into, without which a background (``await_completion=False``)
        build's caller would have no notification of completion at all
        (the immediately-returned ``BuildOutcome`` reflects only "a build
        was scheduled", not its eventual result).
        """
        entry = await self._manifest.get(source_id)
        if entry is not None and entry.state == "clean":
            return BuildOutcome(
                source_id=source_id, triggered=False, background=False,
                chunk_count=entry.chunk_count,
            )
        if entry is not None and entry.state == "building":
            # Someone (this process or another) is already mid-build.
            return BuildOutcome(source_id=source_id, triggered=False, background=False)

        build_fn = self._builders.get(source_id)
        if build_fn is None:
            raise ValueError(
                f"IndexCoordinator.ensure_built({source_id!r}): no builder "
                f"registered — call register_builder(source_id, build_fn) first."
            )

        if not await_completion:
            existing_task = self._bg_tasks.get(source_id)
            if existing_task is not None and not existing_task.done():
                return BuildOutcome(source_id=source_id, triggered=True, background=True)
            task = asyncio.create_task(
                self._run_build(source_id, build_fn, events, on_error, on_success)
            )
            self._bg_tasks[source_id] = task
            return BuildOutcome(source_id=source_id, triggered=True, background=True)

        return await self._run_build(source_id, build_fn, events, on_error, on_success)

    def _emit(self, events: "EventLog | None", event_type: str, **data: Any) -> None:
        """Best-effort audit-event emit (FP-0066 P2d) — never raises; a
        broken/absent sink must not fail a build or a search (mirrors
        ``emit_cli_event``'s "audit-emit failure must not propagate"
        contract). ``event_type`` (not ``kind``) to avoid colliding with
        the ``kind=`` (dynamic/static/backfill taxonomy) keyword every
        build-phase emit also carries as data."""
        if events is None:
            return
        try:
            events.emit(event_type, **data)
        except Exception:
            pass

    async def _run_build(
        self,
        source_id: str,
        build_fn: BuildFn,
        events: "EventLog | None" = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_success: Callable[[BuildOutcome], None] | None = None,
    ) -> BuildOutcome:
        kind = self._kinds.get(source_id, "backfill")
        self._emit(events, "embedding_index_build_started", source_id=source_id, kind=kind)
        await self._set_state(source_id, "building")
        lock_dir = cache_dir_for_source(self._workspace_root, source_id)
        with try_acquire_build_lock(lock_dir) as got_lock:
            if not got_lock:
                # Another process is mid-build — fall back to whatever is
                # on disk rather than duplicating the embed-API cost. This
                # is now the SOLE cross-process build-lock acquisition for
                # every registered source (P2-convergence PR1, #3270 §2):
                # domain adapters (e.g. ``ActionEmbeddingIndex``) no longer
                # hold their own copy of this lock, which is what made the
                # self-deadlock-shaped double-acquire structurally
                # impossible rather than merely avoided.
                return BuildOutcome(source_id=source_id, triggered=False, background=False)
            try:
                material = await build_fn()
                if material is None:
                    # P2-convergence PR1 (#3270 §2): the adapter's OWN
                    # material-generation policy (e.g. a disk-adopt cache
                    # hit) determined no embed+write is needed this call —
                    # mark clean without touching write history. The
                    # manifest's chunk_count/embedding_model are left as
                    # whatever they already were (this path never had a
                    # write to report a fresh count from — not externally
                    # observable: no production caller reads this
                    # BuildOutcome's chunk_count for the action-catalog
                    # source, see ``RouterLoop._ensure_action_index_built``).
                    await self._set_state(source_id, "clean")
                    entry = await self._manifest.get(source_id)
                    chunk_count = entry.chunk_count if entry is not None else None
                    self._emit(
                        events, "embedding_index_build_complete",
                        source_id=source_id, chunk_count=chunk_count,
                    )
                    outcome = BuildOutcome(
                        source_id=source_id, triggered=True, background=False,
                        chunk_count=chunk_count,
                    )
                    if on_success is not None:
                        try:
                            on_success(outcome)
                        except Exception:
                            pass
                    return outcome
                self._emit(
                    events, "embedding_index_build_progress",
                    source_id=source_id, chunk_count=len(material.items),
                )
                result = await embed_verify_write(
                    ctx=material.ctx,
                    texts=material.texts,
                    model_class=material.model_class,
                    items=material.items,
                    to_chunk_record=material.to_chunk_record,
                    backend=self._backend,
                    source=source_id,
                    mode="replace",
                    item_noun="items",
                    label="build",
                )
            except Exception as exc:
                reason = f"build_error: {exc}"
                self._failure_memo[source_id] = reason
                await self.mark_dirty(source_id, reason=reason)
                self._emit(
                    events, "embedding_index_build_error",
                    source_id=source_id, reason=str(exc),
                )
                if on_error is not None:
                    try:
                        on_error(exc)
                    except Exception:
                        pass
                return BuildOutcome(
                    source_id=source_id, triggered=True, background=False, error=str(exc),
                )

        chunk_count = result.write_result["written"]
        await self._set_state(
            source_id, "clean", chunk_count=chunk_count,
            embedding_model=result.resolved_model,
        )
        self._emit(
            events, "embedding_index_build_complete",
            source_id=source_id, chunk_count=chunk_count,
        )
        outcome = BuildOutcome(
            source_id=source_id, triggered=True, background=False, chunk_count=chunk_count,
        )
        if on_success is not None:
            try:
                on_success(outcome)
            except Exception:
                pass
        return outcome

    async def _set_state(
        self,
        source_id: str,
        state: str,
        *,
        chunk_count: int | None = None,
        embedding_model: str | None = None,
    ) -> None:
        entry = await self._manifest.get(source_id)
        if entry is None:
            entry = SourceEntry(
                name=source_id, description="", path="", backend="sqlite",
                kind=self._kinds.get(source_id, "backfill"),
            )
        entry.state = state  # type: ignore[assignment]
        if state == "clean":
            entry.last_error = None
        if chunk_count is not None:
            entry.chunk_count = chunk_count
        if embedding_model is not None:
            entry.embedding_model = embedding_model
        if state == "clean":
            from datetime import datetime, timezone
            entry.last_indexed = datetime.now(timezone.utc).isoformat()
        await self._manifest.upsert(entry)

    # ── search_await (#3247 firm §1 + §5 search-await contract) ─────────

    async def search_await(self, source_id: str) -> None:
        """Await any pending/dirty ingest for ``source_id`` before a search
        returns (the completeness guarantee — "best-effort search is a
        bug").

        Steady-state (``state == "clean"``) is a cheap manifest-read no-op
        — no build is triggered. ``dirty``/``error`` triggers a heal
        (synchronous rebuild via ``ensure_built(await_completion=True)``).
        ``building`` awaits the in-flight background task if this process
        is the one running it (volatile — a DIFFERENT process's in-flight
        build is not awaitable here; the next call after it completes will
        observe ``clean`` via the persisted manifest).
        """
        entry = await self._manifest.get(source_id)
        if entry is None or entry.state == "clean":
            return
        if entry.state == "building":
            task = self._bg_tasks.get(source_id)
            if task is not None:
                await task
            return
        # dirty or error → heal.
        if source_id not in self._builders:
            # No strategy registered in THIS process (e.g. a fresh process
            # after a crash, before P2b/P2c wire real builders). Nothing to
            # heal with — the persisted dirty flag still correctly reports
            # "not ready" via is_ready(); a caller that owns a builder can
            # register it and call ensure_built/search_await again.
            return
        await self.ensure_built(source_id, await_completion=True)

    # ── is_ready (#3247 firm §1) ─────────────────────────────────────────

    async def is_ready(self, source_id: str) -> bool:
        """Readiness gate — True iff the manifest records this source as
        ``clean`` (a completed, current build)."""
        entry = await self._manifest.get(source_id)
        return entry is not None and entry.state == "clean"

    # ── delete_entries (FP-0066 P3a, #3247 firm §4 G3) ───────────────────

    async def delete_entries(self, source_id: str, content_hashes: list[str]) -> int:
        """Sync per-entry de-index — the §G3 completeness guarantee's
        delete-side ("a stale index entry must not survive its source
        content being removed").

        Deliberately NOT the ``index_drop`` op: ``index_drop`` is a
        documented WHOLE-SOURCE drop (removes the entire
        ``SourceManifest`` entry + every row in the backend for
        ``source_id``, no per-item selection) — using it for a single
        forgotten memory entry or one uninstalled skill would destroy
        every OTHER entry sharing the same source too. This method calls
        ``IndexBackend.delete(source_id, content_hashes)`` instead — the
        existing per-row deletion primitive (already used by the
        ``index_update`` op's remove-reconciliation path) — under the SAME
        cross-process ``build_lock`` a concurrent build acquires, so a
        delete cannot race a build's write.

        Raises (does NOT catch/best-effort) if the lock is held by another
        in-flight build: per the firm's §4, de-index is sync, not
        best-effort — silently skipping would leave the stale row
        searchable, exactly the bug this method exists to prevent. The
        caller (``forget_memory``/``plugin_uninstall``) decides how to
        surface that failure (typically as its own error-shaped result).
        """
        if not content_hashes:
            return 0
        lock_dir = cache_dir_for_source(self._workspace_root, source_id)
        with try_acquire_build_lock(lock_dir) as got_lock:
            if not got_lock:
                raise RuntimeError(
                    f"delete_entries({source_id!r}): another process holds "
                    "the build lock — retry once its build completes."
                )
            removed = await self._backend.delete(source_id, content_hashes)
        entry = await self._manifest.get(source_id)
        if entry is not None and removed:
            entry.chunk_count = max(0, entry.chunk_count - removed)
            await self._manifest.upsert(entry)
        return removed

    # ── failure-memo read surface (mirrors router_loop's
    #    ``_action_index_build_failed`` semantics) ───────────────────────

    def build_failed(self, source_id: str) -> bool:
        """True if a prior build attempt in THIS process failed (per-process
        once-per-source memoization, mirrors ``RouterLoop.
        _action_index_build_failed`` — cross-process/cross-restart
        persistence of "don't retry" is intentionally NOT implemented; the
        persisted ``sources.yaml`` state is ``dirty``/``error`` regardless,
        so a fresh process/session gets exactly one retry, which is the
        desired heal path)."""
        return source_id in self._failure_memo

    # ``ensure_built_self_contained`` — the ORCHESTRATION-only twin that used
    # to exist here for a domain adapter (``ActionEmbeddingIndex``) that
    # owned its own cross-process lock + write, so it could not be split
    # into a material-only ``BuildFn`` without either duplicating its
    # disk-adopt/dual-axis-invalidation policy here or double-acquiring the
    # SAME advisory lock within one process (self-deadlock-shaped: a second
    # ``try_acquire_build_lock`` call sees ITS OWN pid as a live holder and
    # silently no-ops) — was ELIMINATED by P2-convergence PR1 (#3270 §2,
    # design firm on #3270). ``ActionEmbeddingIndex.build()`` lost both its
    # locks (P2-convergence PR1's actual fix: the cross-process lock above,
    # acquired once here in ``_run_build``, is now the SOLE holder, making
    # the same-path double-acquire structurally impossible rather than
    # merely avoided) and its policy was extracted into
    # ``ActionEmbeddingIndex.prepare_material`` — a ``BuildFn`` that returns
    # ``BuildMaterial`` (real rebuild) or ``None`` (adapter determined no
    # write is needed, e.g. a disk-adopt cache hit; see ``_run_build``'s
    # ``material is None`` branch above). ``ensure_built`` is now the ONE
    # entry point for every registered source, action-catalog included.

# ── Module-level singleton registry (per-workspace) ───────────────────────
#
# Mirrors ``get_source_manifest`` (FP-0066 P2b, #3247): a session/router-
# scoped caller (``RouterLoop._get_index_coordinator``) needs the SAME
# ``IndexCoordinator`` instance across turns within a chain (and across
# chains within the same workspace) so ``_bg_tasks``/``_failure_memo``
# once-per-source dedup actually dedups rather than resetting every call.

_COORDINATORS: dict[Path, IndexCoordinator] = {}


def get_index_coordinator(workspace_root: Path) -> IndexCoordinator:
    """Get or create the IndexCoordinator singleton for a workspace."""
    workspace_root = workspace_root.resolve()
    if workspace_root not in _COORDINATORS:
        _COORDINATORS[workspace_root] = IndexCoordinator(workspace_root)
    return _COORDINATORS[workspace_root]


# ── search-emit helper (P3-helper, #3247 firm §6) ─────────────────────────
#
# `semantic_search_started -> search_await -> query -> semantic_search_
# complete` was duplicated verbatim at the two live query call sites
# (RouterLoop.search_actions in router_loop.py, universal_catalog.
# _handle_search_actions) — a third caller (search_knowledge, P3c) would
# have made it three, so the firm calls for a single-source helper BEFORE
# that lands (do not let the dup become an established pattern).
#
# ``coordinator``/``op_ctx`` are argument-injected rather than resolved
# inside the helper: the two call sites acquire them asymmetrically
# (RouterLoop via ``self._get_index_coordinator()`` + ``self.host.
# make_router_op_context()``; the catalog handler via ``get_index_
# coordinator(ctx.workspace.base_dir)`` gated on ``ctx.workspace`` being
# set + ``rs.op_context_factory()``) — the helper must not special-case
# either acquisition path, so it never calls ``get_index_coordinator``
# itself. ``coordinator=None`` is honored as "skip search_await" (the
# catalog site's pre-existing degrade when ``ctx.workspace`` is unset).
# ``events`` is None-tolerant for the same reason (the catalog handler
# may be invoked without an events sink).
#
# ★ Bug fix this extraction exposes: the catalog call site had NO
# try/finally around ``index.query()`` — a query failure emitted
# ``semantic_search_started`` but never its matching ``_complete``, a
# dangling started-without-complete in the audit trail (undetected
# because nothing previously asserted the pairing under failure). The
# router_loop site already wrapped the query in try/finally (with its
# own best-effort except/log around the whole thing); this helper folds
# that guarantee in ONE place so both sites get it uniformly — on any
# exception raised by ``search_await``/``index.query``, ``_complete``
# still fires (with ``results=0``) before the exception re-raises to the
# caller, which decides for itself whether to swallow it (best-effort
# presentation, per ``RouterLoop.search_actions``) or propagate it
# (the catalog handler, unchanged from before).
async def emit_wrapped_semantic_search(
    *,
    events: "EventLog | None",
    coordinator: "IndexCoordinator | None",
    source_id: str,
    index: Any,
    query: str,
    op_ctx: "OpContext",
    model_class: str,
    top_k: int,
) -> list[dict[str, Any]]:
    """Unified ``semantic_search_started`` -> ``search_await`` -> ``query``
    -> ``semantic_search_complete`` wrap (#3247 firm §6). Guarantees
    ``semantic_search_complete`` always fires (``results=0`` on failure)
    even when ``search_await``/``index.query`` raises, then re-raises so
    the caller retains its own error-handling policy."""
    if events is not None:
        events.emit("semantic_search_started", source_id=source_id)
    results: list[dict[str, Any]] = []
    try:
        if coordinator is not None:
            await coordinator.search_await(source_id)
        results = await index.query(query, op_ctx, model_class, top_k=top_k)
    finally:
        if events is not None:
            events.emit(
                "semantic_search_complete", source_id=source_id, results=len(results),
            )
    return results
