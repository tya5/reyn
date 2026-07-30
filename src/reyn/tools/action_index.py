"""ActionEmbeddingIndex — tool-use semantic index, riding the unified IndexBackend.

FP-0034 §D13 / §D15 spec — Phase 2 step 2 added SQLite-WAL persistence so
that re-embedding is skipped across process restarts when the catalog has
not changed. FP-0057 Phase 0 (#2843) folds the storage/cosine/lock layer
that used to be hand-rolled here onto the pluggable ``IndexBackend`` (the
same substrate OS-internal RAG ingestion — the ``index_update`` op — and
query — ``semantic_search`` — ride; the safe-mode ``reyn.api.safe.
index_update`` user-facing wrapper that used to sit in front of the op was
retired FP-0066 P1c) — this class is now a thin **domain adapter**: it owns
the action-catalog-
specific dual-axis (catalog-hash + model-class) invalidation policy and
delegates vector storage, cosine ranking, and content-hash dedup to the
backend. What moved out (single canonical implementation now, no more
hand-rolled duplicate):

  - Cosine similarity: was a hand-rolled ``math.sqrt`` loop here; now the
    backend's numpy cosine (``SqliteIndexBackend.query``).
  - Cross-process build-coordination PID advisory lock: moved to
    ``reyn.data.index.build_lock`` (shared with ``SourceManifest``'s
    raise-on-contention lock — same PID-liveness/marker-file primitives,
    two different contention policies).
  - On-disk schema + per-chunk dedup: was a private ``meta``/``vectors``
    SQLite schema here; now the unified ``chunks``/``meta`` schema
    ``SqliteIndexBackend`` already uses for doc-RAG sources.

Clean-break: the old ``.reyn/cache/action_index/`` directory is no longer
read or written. Storage now lives at the unified convention
``.reyn/cache/index/<source>/`` (default ``source="actions"``), so the
first build after upgrading rebuilds from scratch at the new path — no
migration code, since the action index is cache (regenerable, not
recovery-core). See ``docs/reference/runtime/reyn-dir-layout.md``.

Lifecycle:
  1. Construction: empty index, ``is_ready() == False``.
  2. ``await build(items, ctx, model_class)`` — embeds each item's
     ``"{action_name}: {short_description}"`` text via
     ``execute_op(EmbedIROp(...), ctx)`` (FP-0057 #2856 Part A — the shared
     `embed` op, not a provider-direct call), stores the vectors via the
     backend, and records a catalog snapshot hash. On completion
     ``is_ready()`` returns True.
     Disk shortcut: when the on-disk backend state already carries the
     same catalog hash + model class, the embed call is skipped and the
     in-memory state is adopted from disk (= process-restart cache hit).
  3. ``await query(text, ctx, model_class, top_k=10)`` — embeds
     the query once (same `embed`-op route), asks the backend to rank all
     stored vectors by cosine similarity, and returns the top-K items with
     their ``score``. When the index is not ready, returns ``[]`` so
     callers (= ``search_actions`` handler) gracefully degrade instead of
     crashing.

FP-0057 #2856 Part A (redaction-bypass close-out): ``build()``/``query()``
used to call ``provider.embed(...)`` PROVIDER-DIRECT, carrying a session-
scoped provider — a bypass of the shared `embed` op's PRE-embed
redaction-egress scan (a secret in the tool catalog's
``short_description`` would previously leave the process unredacted).
Both methods now take an ``OpContext`` instead of an ``EmbeddingProvider``
and route through ``execute_op(EmbedIROp(...), ctx)`` (see
``core/op_runtime/embed.py``). (#3438 removed the ``ctx.embedding_event_sink``
TUI model-download-status forwarding this comment used to describe — it had
no producer; no provider in the repo ever accepted the ``event_sink`` kwarg
``get_provider`` conditionally forwarded, and the original reason for it —
a local in-process embedding model's lazy-load lifecycle, FP-0043 C.3 —
stopped applying once #3128 removed that in-process backend.)

Catalog hash semantics:
  - Hash is over the SORTED tuple of action_names.
  - When ``build()`` is called with the same hash AND model class, it is
    a no-op (= idempotent reload guard).
  - Different hash, or same hash but a different model class → rebuild
    (a full re-embed; Phase 0 keeps the existing all-or-nothing rebuild
    policy — per-item incremental reconcile is Phase 2's ``index_update``,
    not built here).

Concurrency (P2-convergence PR1, #3270 §2 — REVISED, both locks REMOVED
from this class): ``build()``/``prepare_material()`` are now lock-free.
Production builds route through ``IndexCoordinator.ensure_built`` (via
``register_builder`` — see ``RouterLoop._ensure_action_index_built``),
which is the SOLE holder of the cross-process advisory build lock
(``reyn.data.index.build_lock.try_acquire_build_lock``, acquired once in
``IndexCoordinator._run_build``) — a live holder there means "another
process is mid-build", falling back to whatever's on disk instead of
duplicating the embed-API cost of a concurrent rebuild (#3128: embeddings
are litellm API calls, not an in-process model load). Same-instance
concurrent-call serialization is the Coordinator's ``_bg_tasks``
once-per-source dedup responsibility, not this class's. A direct/
standalone ``build()`` call (bypassing the Coordinator — tests only in
production code today) gets neither guarantee, matching a plain async
method's normal semantics.

Catalog coverage (FP-0057 Phase 2b re-check): today's catalog covers
primitive tools, MCP tools, and pipelines. There is no separate per-skill
runtime-invoke category to add — the skill ENGINE was deleted (#2438);
``universal_catalog.CATEGORIES`` only carries ``skill_management`` (the
install-plane), never a per-skill dynamic-dispatch category — so the prior
"NOT skills" gap note no longer describes a live extension point. The
``source``/``kind`` metadata captured on every chunk (``extra["kind"]``,
derived from the action_name's category prefix) still keeps the door
open for a future per-kind source split or filter without requiring a
storage-layer rewrite, should a per-skill invoke category ever return.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from reyn.data.index import IndexBackend, get_backend
from reyn.data.index.backend import ChunkRecord, cache_dir_for_source
from reyn.data.index.coordinator import BuildMaterial, embed_verify_write

if TYPE_CHECKING:
    from reyn.core.op_runtime.context import OpContext

# Default logical source name the action catalog rides on the unified
# IndexBackend. A single instance's catalog is written with mode="replace"
# on every rebuild (matches the pre-consolidation all-or-nothing semantics);
# a future Phase 2 kind-split could parameterise this per invocable kind.
DEFAULT_ACTION_SOURCE = "actions"


def compute_catalog_hash(items: list[Mapping[str, Any]]) -> str:
    """Snapshot hash over the action_name set.

    Stable to ordering, since the items list is sorted before hashing.
    Used as the rebuild trigger: same hash → no-op build.
    """
    names = sorted(
        str(it.get("action_name", ""))
        for it in items
        if it.get("action_name")
    )
    joined = "\n".join(names)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _split_category(action_name: str) -> str:
    """Best-effort category for metadata/kind hints only.

    Reads ``universal_dispatch.category_of``, returning ``""`` for a name the
    catalog does not know — a soft hint stored in ``ChunkMetadata.extra`` for
    future kind-based filtering, never correctness-critical, so an unknown name
    must degrade rather than raise (test fixtures and future kinds may index
    names outside the current action set).

    #3429: it used to take the prefix before ``__``, which was the category
    only because a name's category was baked into its spelling. The category is
    now a table lookup.
    """
    from reyn.tools.universal_dispatch import category_of

    return category_of(action_name) or ""


class ActionEmbeddingIndex:
    """Domain adapter over ``IndexBackend`` for the tool-use action catalog.

    Holds no vectors itself — build()/query() delegate storage, cosine
    ranking, and per-chunk dedup to the configured ``IndexBackend``
    (default: the registered ``"sqlite"`` backend, unified with doc-RAG's
    ``SqliteIndexBackend``). This class owns only the action-catalog
    domain policy: the whole-catalog-hash + model-class dual-axis
    invalidation, and the item<->ChunkRecord mapping.

    Production wiring:
      - One instance per Session (= router-scoped).
      - RouterLoop bootstraps an async ``build()`` task on first turn
        when ``embedding.enabled: true`` (FP-0066 §7).
      - ``search_actions`` handler delegates to ``query()`` when
        ``is_ready()`` returns True; otherwise returns an empty result.
      - ``workspace_root`` defaults to ``Path.cwd()`` (mirrors
        ``SqliteIndexBackend``'s own default) — storage lands at
        ``<workspace_root>/.reyn/cache/index/<source>/``.
    """

    def __init__(
        self,
        workspace_root: Path | None = None,
        *,
        source: str = DEFAULT_ACTION_SOURCE,
        backend: IndexBackend | None = None,
    ) -> None:
        self._workspace_root = (
            workspace_root if workspace_root is not None else Path.cwd()
        )
        self._source = source
        self._backend: IndexBackend = (
            backend
            if backend is not None
            else get_backend("sqlite", workspace_root=self._workspace_root)
        )
        self._catalog_hash: str | None = None
        self._model_class: str | None = None  # FP-0043 Component E: class-swap detection
        self._size: int = 0
        self._building = False

    # ── identity ────────────────────────────────────────────────────────

    @property
    def source_name(self) -> str:
        """The logical source id this instance rides on the IndexBackend /
        IndexCoordinator (FP-0066 P2b, #3247) — ``"actions"`` by default."""
        return self._source

    # ── paths ───────────────────────────────────────────────────────────

    @property
    def db_path(self) -> Path | None:
        """Conventional on-disk location, for CLI/debug/test introspection.

        Assumes the default sqlite-shaped backend layout (``index.db``
        under the unified per-source cache dir); a hypothetical alternate
        in-core backend without a local file would make this meaningless,
        but Phase 0 only registers the sqlite backend so this stays
        accurate today.
        """
        return cache_dir_for_source(self._workspace_root, self._source) / "index.db"

    def _catalog_meta_path(self) -> Path:
        """Sidecar carrying the whole-catalog hash (not tracked by IndexBackend).

        A single small JSON file, not a second schema — ``IndexBackend``
        already tracks per-chunk ``embedding_model``/``last_indexed`` via
        ``stat()``; this sidecar carries the one action-specific value
        (the whole-catalog snapshot hash) the protocol has no slot for.
        """
        return cache_dir_for_source(self._workspace_root, self._source) / "catalog_meta.json"

    def _read_catalog_meta_hash(self) -> str | None:
        try:
            data = json.loads(self._catalog_meta_path().read_text(encoding="utf-8"))
            h = data.get("catalog_hash")
            return str(h) if h else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _write_catalog_meta_hash(self, catalog_hash: str) -> None:
        try:
            path = self._catalog_meta_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"catalog_hash": catalog_hash}), encoding="utf-8"
            )
        except OSError:
            pass  # best-effort write-through cache; in-memory state stays authoritative

    # ── public read surface ────────────────────────────────────────────

    def is_ready(self) -> bool:
        """Return True iff the index has a completed build available.

        Used by ``search_actions`` handler visibility gating (§D14) and
        by ``build_tools`` to decide whether to expose the wrapper to
        the LLM at all.
        """
        return self._catalog_hash is not None and not self._building

    def catalog_hash(self) -> str | None:
        """Return the recorded catalog snapshot hash, or None pre-build."""
        return self._catalog_hash

    @property
    def model_class(self) -> str | None:
        """Return the model class associated with the current vectors, or None.

        FP-0043 Component E: paired with ``catalog_hash`` as a two-axis
        cache key. A change in either axis triggers rebuild on the next
        ``build()`` call.
        """
        return self._model_class

    def size(self) -> int:
        """Return the number of indexed items (= vectors stored)."""
        return self._size

    # ── external-write state sync (P2-convergence PR1, #3270 §2) ────────

    def adopt_build_result(
        self, catalog_hash: str, model_class: str, chunk_count: int,
    ) -> None:
        """Sync in-memory state (+ the on-disk catalog-hash sidecar) after
        a CALLER-PERFORMED write succeeded.

        The Coordinator-driven counterpart to ``build()``'s own post-write
        update: when ``prepare_material`` returns real material and the
        CALLER (``IndexCoordinator._run_build``, via
        ``IndexCoordinator.ensure_built``) performs the actual
        ``embed_verify_write`` — rather than this instance performing it
        itself, as ``build()`` still does for a direct/standalone call —
        this instance's own ``is_ready()``/``size()``/``catalog_hash()``
        gate needs updating from that external result. Kept here (not
        inlined at the call site, ``RouterLoop._ensure_action_index_built``)
        so the state mutation + sidecar write stay ONE canonical
        implementation, shared with ``build()``'s own internal update
        (below) rather than two hand-maintained copies.
        """
        self._catalog_hash = catalog_hash
        self._model_class = model_class
        self._size = chunk_count
        self._write_catalog_meta_hash(catalog_hash)

    # ── item <-> ChunkRecord mapping ───────────────────────────────────

    def _to_chunk_record(
        self, item: Mapping[str, Any], vector: list[float], model_class: str,
    ) -> ChunkRecord:
        qn = str(item["action_name"])
        category = _split_category(qn)
        content_hash = hashlib.sha256(qn.encode("utf-8")).hexdigest()
        metadata: dict[str, Any] = {
            "source_path": qn,
            "source_type": "action",
            "content_hash": content_hash,
            # The dual-axis invalidation compares this against the caller's
            # model_class (not the provider's resolved literal model id) —
            # matches the pre-consolidation FP-0043 Component E semantics.
            "embedding_model": model_class,
            "chunk_index": 0,
            "size_tokens": 0,
            "parent_context": category or None,
            "extra": {"action_item": dict(item), "kind": category},
        }
        text = f"{qn}: {item.get('short_description', '')}"
        return ChunkRecord(text=text, vector=list(vector), metadata=metadata, score=None)

    # ── build ───────────────────────────────────────────────────────────

    async def _try_adopt_from_disk(
        self, expected_hash: str, expected_model_class: str,
    ) -> bool:
        """Adopt in-memory state from the backend when BOTH axes match.

        Returns True and updates ``_catalog_hash``/``_model_class``/``_size``
        when the on-disk sidecar's catalog hash AND the backend's persisted
        embedding_model both match the expectations. Returns False
        (without mutating state) otherwise — including when the backend is
        empty (no prior build at this source/workspace).
        """
        stored_hash = self._read_catalog_meta_hash()
        if stored_hash is None or stored_hash != expected_hash:
            return False
        stat = await self._backend.stat(self._source)
        if stat["embedding_model"] != expected_model_class:
            return False
        self._catalog_hash = expected_hash
        self._model_class = expected_model_class
        self._size = stat["chunk_count"]
        return True

    async def _embed_via_op(
        self, texts: list[str], ctx: "OpContext", model_class: str,
    ) -> list[list[float]]:
        """Embed ``texts`` via the shared `embed` op (FP-0057 #2856 Part A).

        Replaces the pre-#2856 ``provider.embed(...)`` provider-direct call —
        routing through ``execute_op`` inherits the op's PRE-embed redaction-
        egress scan (``embed.py``'s co-vet #3 seam) instead of bypassing it.

        Raises ``RuntimeError`` on an op-level failure (mirrors the previous
        provider-direct exception-propagation contract — ``build()``'s
        all-or-nothing partial-build guard depends on this raising rather
        than returning a partial/empty vector list silently).
        """
        from reyn.core.op_runtime import execute_op
        from reyn.schemas.models import EmbedIROp

        result = await execute_op(
            EmbedIROp(kind="embed", texts=texts, embedding_model=model_class), ctx,
        )
        if result.get("status") == "error":
            raise RuntimeError(f"embed op failed: {result.get('error')}")
        return list(result.get("vectors", []))

    async def prepare_material(
        self,
        items: list[Mapping[str, Any]],
        ctx: "OpContext",
        model_class: str,
    ) -> "BuildMaterial | None":
        """Lock-free, write-free material generation — the ``BuildFn``
        ``IndexCoordinator.register_builder``/``ensure_built`` calls
        (P2-convergence PR1, #3270 §2). Owns the action-catalog's
        disk-adopt + dual-axis (catalog-hash + model-class) invalidation
        POLICY — kept here (not moved to the Coordinator) because it is
        part of the domain adapter's own material-generation decision,
        per the firm's boundary principle.

        Returns ``None`` when no rebuild is needed this call — either the
        in-memory state already matches (both axes), or a disk-adopt cache
        hit applies (this method mutates ``_catalog_hash``/``_model_class``/
        ``_size`` directly in that case, exactly as the pre-PR1 ``build()``
        did). Returns a ``BuildMaterial`` when a real embed+write is
        needed; the caller (``build()`` for a direct/standalone call, or
        the Coordinator's ``_run_build`` for the production path via
        ``ensure_built``) owns performing the actual
        ``embed_verify_write`` and, on success, updating this instance's
        in-memory state — see ``build()`` below and
        ``RouterLoop._ensure_action_index_built``.

        No cross-process lock here BY DESIGN: the ONLY production entry
        point (``IndexCoordinator.ensure_built``) already holds the SAME
        advisory lock (``try_acquire_build_lock`` at the same
        ``cache_dir_for_source`` path) for the ENTIRE duration this method
        runs (it is called from inside the Coordinator's own
        ``with try_acquire_build_lock(...)`` block in ``_run_build``) — a
        second acquisition here would be the self-deadlock-shaped bug this
        PR removes (the second call would see its OWN pid as the live
        holder and silently skip). A direct/standalone caller (bypassing
        the Coordinator, e.g. a test) gets no cross-process protection —
        that guarantee is now Coordinator-exclusive.
        """
        new_hash = compute_catalog_hash(list(items))
        if new_hash == self._catalog_hash and self._model_class == model_class:
            return None  # idempotent (in-memory match on BOTH axes)

        if await self._try_adopt_from_disk(new_hash, model_class):
            return None  # cache hit — skip embed call

        valid_items = sorted(
            (dict(it) for it in items if it.get("action_name")),
            key=lambda it: str(it["action_name"]),
        )
        texts = [
            f"{it['action_name']}: {it.get('short_description', '')}"
            for it in valid_items
        ]
        return BuildMaterial(
            items=valid_items,
            texts=texts,
            to_chunk_record=lambda it, v, _resolved: self._to_chunk_record(
                it, v, model_class,
            ),
            model_class=model_class,
            ctx=ctx,
        )

    async def build(
        self,
        items: list[Mapping[str, Any]],
        ctx: "OpContext",
        model_class: str,
    ) -> None:
        """Embed each item and store the vector via the backend.

        Each item must carry ``action_name`` and optionally
        ``short_description``.  The embedded text is
        ``"{action_name}: {short_description}"`` so both the
        category-prefixed name and the human-readable summary
        contribute to the embedding.

        Unified build trigger (FP-0043 Component E): the call is
        idempotent in three orthogonal ways —

          1. catalog hash matches AND model class matches  → no-op
          2. catalog hash matches BUT model class differs  → rebuild
             (class-swap invalidates vectors from the previous model)
          3. catalog hash differs                          → rebuild

        Lock-free (P2-convergence PR1, #3270 §2): this method — the
        material-generation POLICY (``prepare_material``, above) plus the
        embed+write it performs when a real rebuild is needed — no longer
        holds any lock (neither the prior in-process ``asyncio.Lock`` nor
        the cross-process advisory ``build_lock``). Same-instance
        concurrent-call serialization is now the caller's responsibility
        (production callers go through ``IndexCoordinator.ensure_built``,
        whose ``_bg_tasks`` once-per-source dedup + cross-process
        ``build_lock`` — the SOLE acquisition, see ``prepare_material``'s
        docstring — cover it); a direct/standalone call (as this method
        remains, for tests and non-Coordinator callers) gets no locking at
        all, matching a plain async method's normal semantics.

        FP-0057 #2856 Part A: ``ctx`` (an ``OpContext``) replaces the prior
        ``provider`` (``EmbeddingProvider``) argument — the embed call now
        routes through ``execute_op(EmbedIROp(...), ctx)`` (see
        ``_embed_via_op``) instead of calling a caller-held provider
        directly.
        """
        material = await self.prepare_material(items, ctx, model_class)
        if material is None:
            return  # already satisfied — see prepare_material's docstring

        new_hash = compute_catalog_hash(list(items))
        self._building = True
        try:
            # FP-0066 P2a (#3247): the embed+verify+write step is the
            # ONE canonical all-or-nothing implementation, shared
            # with the `index_update` op (which used to duplicate
            # this verbatim) — see
            # ``reyn.data.index.coordinator.embed_verify_write``. If this
            # raises (e.g. a mismatched vector count), ``adopt_build_result``
            # below is never reached — in-memory state stays exactly as it
            # was pre-call (the all-or-nothing refusal-of-partial-build
            # guarantee), matching pre-PR1 behavior.
            result = await embed_verify_write(
                ctx=material.ctx,
                texts=material.texts,
                model_class=material.model_class,
                items=material.items,
                to_chunk_record=material.to_chunk_record,
                backend=self._backend,
                source=self._source,
                mode="replace",
                item_noun="items",
                label="build",
            )
            self.adopt_build_result(new_hash, model_class, result.write_result["written"])
        finally:
            self._building = False

    # ── query ───────────────────────────────────────────────────────────

    async def query(
        self,
        query_text: str,
        ctx: "OpContext",
        model_class: str,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Return top-K items ranked by cosine similarity to the query.

        Each result item carries the original item fields plus a
        ``score`` float in ``[-1.0, 1.0]`` (typical embedding range
        ``[0.0, 1.0]``; negative scores are uncommon but possible).
        When the index is not ready (= build incomplete or absent),
        returns an empty list so the caller (= search_actions handler)
        gracefully degrades.

        Empty / whitespace-only query → empty result.

        FP-0057 #2856 Part A: ``ctx`` (an ``OpContext``) replaces the prior
        ``provider`` (``EmbeddingProvider``) argument — see ``build()``'s
        docstring / ``_embed_via_op``.
        """
        if not self.is_ready():
            return []
        if not query_text or not query_text.strip():
            return []
        if top_k <= 0:
            return []

        query_vectors = await self._embed_via_op([query_text], ctx, model_class)
        if not query_vectors:
            return []
        query_vec = query_vectors[0]

        records = await self._backend.query(
            self._source, query_vec, top_k, filters={},
        )

        out: list[dict[str, Any]] = []
        for rec in records:
            extra = rec["metadata"].get("extra") or {}
            item = dict(extra.get("action_item") or {})
            item["score"] = rec["score"]
            out.append(item)
        return out


__all__ = [
    "ActionEmbeddingIndex",
    "compute_catalog_hash",
    "DEFAULT_ACTION_SOURCE",
]
