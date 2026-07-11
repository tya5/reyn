"""ActionEmbeddingIndex — tool-use semantic index, riding the unified IndexBackend.

FP-0034 §D13 / §D15 spec — Phase 2 step 2 added SQLite-WAL persistence so
that re-embedding is skipped across process restarts when the catalog has
not changed. FP-0057 Phase 0 (#2843) folds the storage/cosine/lock layer
that used to be hand-rolled here onto the pluggable ``IndexBackend`` (the
same substrate doc-RAG ingestion — ``index_update`` / the safe-mode
``reyn.api.safe.index_update``, FP-0057 Phase 2b — and query — ``semantic_search``
— ride) — this class is now a thin **domain adapter**: it owns the action-catalog-
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
     ``"{qualified_name}: {short_description}"`` text via
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
scoped provider whose only reason for living on this class was to also
carry the TUI's model-download-status ``event_sink`` — a bypass of the
shared `embed` op's PRE-embed redaction-egress scan (a secret in the tool
catalog's ``short_description`` would previously leave the process
unredacted). Both methods now take an ``OpContext`` instead of an
``EmbeddingProvider`` and route through ``execute_op(EmbedIROp(...), ctx)``;
the event_sink is preserved via ``ctx.embedding_event_sink`` (a callable,
not a provider instance) forwarded to the op's own fresh provider
resolution (see ``core/op_runtime/embed.py``).

Catalog hash semantics:
  - Hash is over the SORTED tuple of qualified_names.
  - When ``build()`` is called with the same hash AND model class, it is
    a no-op (= idempotent reload guard).
  - Different hash, or same hash but a different model class → rebuild
    (a full re-embed; Phase 0 keeps the existing all-or-nothing rebuild
    policy — per-item incremental reconcile is Phase 2's ``index_update``,
    not built here).

Concurrency:
  - ``_build_lock`` serialises concurrent ``build()`` calls on this
    instance; the second call awaits the first.
  - Cross-process coordination uses the shared advisory build lock
    (``reyn.data.index.build_lock.try_acquire_build_lock``) — a live
    holder means "another process is mid-build", so this call falls back
    to whatever's on disk instead of duplicating the embed-API cost /
    duplicate sentence-transformers model load.

Catalog coverage (FP-0057 Phase 2b re-check): today's catalog covers
primitive tools, MCP tools, and pipelines. There is no separate per-skill
runtime-invoke category to add — the skill ENGINE was deleted (#2438);
``universal_catalog.CATEGORIES`` only carries ``skill_management`` (the
install-plane), never a per-skill dynamic-dispatch category — so the prior
"NOT skills" gap note no longer describes a live extension point. The
``source``/``kind`` metadata captured on every chunk (``extra["kind"]``,
derived from the qualified_name's category prefix) still keeps the door
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
from reyn.data.index.build_lock import try_acquire_build_lock

if TYPE_CHECKING:
    from reyn.core.op_runtime.context import OpContext

import asyncio

# Default logical source name the action catalog rides on the unified
# IndexBackend. A single instance's catalog is written with mode="replace"
# on every rebuild (matches the pre-consolidation all-or-nothing semantics);
# a future Phase 2 kind-split could parameterise this per invocable kind.
DEFAULT_ACTION_SOURCE = "actions"


def compute_catalog_hash(items: list[Mapping[str, Any]]) -> str:
    """Snapshot hash over the qualified_name set.

    Stable to ordering, since the items list is sorted before hashing.
    Used as the rebuild trigger: same hash → no-op build.
    """
    names = sorted(
        str(it.get("qualified_name", ""))
        for it in items
        if it.get("qualified_name")
    )
    joined = "\n".join(names)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _split_category(qualified_name: str) -> str:
    """Best-effort category prefix for metadata/kind hints only.

    Deliberately NOT ``universal_catalog.split_qualified_name`` — that
    helper raises ``ValueError`` for any category outside the strict
    ``CATEGORIES`` set, which is too strict for an optional metadata
    field (test fixtures and future kinds may use qualified_names that
    don't (yet) validate against that set). This is a soft hint stored
    in ``ChunkMetadata.extra`` for future kind-based filtering — never
    correctness-critical.
    """
    if "__" not in qualified_name:
        return ""
    return qualified_name.split("__", 1)[0]


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
        when ``action_retrieval.embedding_class`` is configured.
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
        self._build_lock = asyncio.Lock()
        self._building = False

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

    # ── item <-> ChunkRecord mapping ───────────────────────────────────

    def _to_chunk_record(
        self, item: Mapping[str, Any], vector: list[float], model_class: str,
    ) -> ChunkRecord:
        qn = str(item["qualified_name"])
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
        ``ctx.embedding_event_sink`` (forwarded by the caller) still reaches
        the op's freshly-resolved provider, so the TUI model-download status
        rows are unaffected by this routing change.

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

    async def build(
        self,
        items: list[Mapping[str, Any]],
        ctx: "OpContext",
        model_class: str,
    ) -> None:
        """Embed each item and store the vector via the backend.

        Each item must carry ``qualified_name`` and optionally
        ``short_description``.  The embedded text is
        ``"{qualified_name}: {short_description}"`` so both the
        category-prefixed name and the human-readable summary
        contribute to the embedding.

        Unified build trigger (FP-0043 Component E): the call is
        idempotent in three orthogonal ways —

          1. catalog hash matches AND model class matches  → no-op
          2. catalog hash matches BUT model class differs  → rebuild
             (class-swap invalidates vectors from the previous model)
          3. catalog hash differs                          → rebuild

        Cross-process build coordination: a non-blocking advisory file
        lock (shared ``reyn.data.index.build_lock``) coordinates
        concurrent builds across OS processes. If another live process
        is mid-build, this call falls back to the disk state without
        invoking the embedding provider.

        FP-0057 #2856 Part A: ``ctx`` (an ``OpContext``) replaces the prior
        ``provider`` (``EmbeddingProvider``) argument — the embed call now
        routes through ``execute_op(EmbedIROp(...), ctx)`` (see
        ``_embed_via_op``) instead of calling a caller-held provider
        directly.
        """
        async with self._build_lock:
            new_hash = compute_catalog_hash(list(items))
            if (
                new_hash == self._catalog_hash
                and self._model_class == model_class
            ):
                return  # idempotent (in-memory match on BOTH axes)

            if await self._try_adopt_from_disk(new_hash, model_class):
                return  # cache hit — skip embed call

            lock_dir = cache_dir_for_source(self._workspace_root, self._source)
            with try_acquire_build_lock(lock_dir) as got_lock:
                if not got_lock:
                    # Another process is building. We can't safely block
                    # the event loop on a sync lock; surface our current
                    # state (likely empty or stale) and let the next
                    # call observe the in-progress process's result.
                    return

                # Re-check disk under the lock — another process may
                # have completed between our pre-lock check and now.
                if await self._try_adopt_from_disk(new_hash, model_class):
                    return

                valid_items = sorted(
                    (dict(it) for it in items if it.get("qualified_name")),
                    key=lambda it: str(it["qualified_name"]),
                )
                if not valid_items:
                    await self._backend.write(self._source, [], mode="replace")
                    self._catalog_hash = new_hash
                    self._model_class = model_class
                    self._size = 0
                    self._write_catalog_meta_hash(new_hash)
                    return

                texts = [
                    f"{it['qualified_name']}: {it.get('short_description', '')}"
                    for it in valid_items
                ]

                self._building = True
                _built_ok = False
                try:
                    vectors = await self._embed_via_op(texts, ctx, model_class)
                    if len(vectors) != len(valid_items):
                        raise RuntimeError(
                            f"EmbeddingProvider returned {len(vectors)} "
                            f"vectors for {len(valid_items)} items; "
                            f"refusing partial build"
                        )
                    records = [
                        self._to_chunk_record(it, v, model_class)
                        for it, v in zip(valid_items, vectors)
                    ]
                    write_result = await self._backend.write(
                        self._source, records, mode="replace",
                    )
                    self._catalog_hash = new_hash
                    self._model_class = model_class
                    self._size = write_result["written"]
                    _built_ok = True
                finally:
                    self._building = False
                if _built_ok:
                    self._write_catalog_meta_hash(new_hash)

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
