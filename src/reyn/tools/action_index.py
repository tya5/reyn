"""ActionEmbeddingIndex — in-memory + SQLite-WAL semantic index for FP-0034.

FP-0034 §D13 / §D15 spec — Phase 2 step 2 adds SQLite-WAL persistence so
that re-embedding is skipped across process restarts when the catalog has
not changed.

Lifecycle:
  1. Construction: empty index, ``is_ready() == False``.
     Pass ``persist_dir`` to enable SQLite persistence (Phase 2 step 2).
  2. ``await build(items, provider, model_class)`` — embeds each item's
     ``"{qualified_name}: {short_description}"`` text via the
     ``EmbeddingProvider``, stores the vectors keyed by qualified_name,
     and records a catalog snapshot hash. On completion ``is_ready()``
     returns True.
     Disk shortcut: when ``persist_dir`` is set and the on-disk DB already
     contains the same catalog hash, the embed call is skipped and vectors
     are loaded from SQLite (= process-restart cache hit).
  3. ``await query(text, provider, model_class, top_k=10)`` — embeds
     the query once, ranks all stored vectors by cosine similarity,
     and returns the top-K items with their ``score``. When the index
     is not ready, returns ``[]`` so callers (= ``search_actions``
     handler) gracefully degrade instead of crashing.

Catalog hash semantics:
  - Hash is over the SORTED tuple of qualified_names.
  - When ``build()`` is called with the same hash, it is a no-op
    (= idempotent reload guard).
  - Different hash → rebuild (full re-embed + persist).

Concurrency:
  - ``_build_lock`` serialises concurrent ``build()`` calls; the second
    call awaits the first.  No partial-state visibility while building
    (``is_ready()`` stays False until the in-progress build completes).
  - ``query()`` reads ``_vectors`` / ``_items`` without locking — Python's
    GIL gives single-statement atomic dict reads; concurrent ``build()``
    is bounded by ``is_ready()`` being False so production callers
    skip ``query()`` while a build is in flight.

SQLite persistence (= Phase 2 step 2, active when ``persist_dir`` is set):
  - DB path: ``<persist_dir>/index.db`` with WAL mode.
  - Schema: ``meta(key, value)`` for catalog_hash; ``vectors(qualified_name,
    item_json, vector_blob)`` for per-item state.
  - Vectors stored as raw ``struct.pack`` double blobs (fast, no JSON float
    round-trip loss).
  - Persistence failures are swallowed — the in-memory state is always
    authoritative; disk is an optimistic write-through cache.

Not yet implemented (= Phase 2 step 3+):
  - ``ActionUsageTracker`` for hot-list ranking
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping

if TYPE_CHECKING:
    from reyn.data.embedding.provider import EmbeddingProvider


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether a PID corresponds to a live process.

    On POSIX, ``os.kill(pid, 0)`` raises ProcessLookupError when the PID
    is gone and PermissionError when it exists but is not ours; both
    mean "still alive enough to defer to". Windows is best-effort.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


@contextlib.contextmanager
def _try_acquire_build_lock(persist_dir: Path) -> Iterator[bool]:
    """Advisory cross-process build lock — non-blocking, take-or-skip.

    Writes a marker file at ``<persist_dir>/.build.lock`` carrying
    ``{pid, ts}``. The contract:

      - If the file is absent OR the previous holder's PID is dead,
        we take the lock and yield ``True``. The caller proceeds with
        the build and the marker is removed on exit.
      - If a live holder is detected, we yield ``False`` immediately
        (= no waiting, no embed-call duplication). The caller is
        expected to either fall back to whatever's already on disk or
        skip the build entirely and let the next attempt observe the
        finished state.

    Atomicity: uses ``O_CREAT | O_EXCL`` for the take so two processes
    racing the take produce exactly one winner. A subsequent stale-PID
    reap is also atomic (unlink + re-take).

    Filesystem-write errors fall through with ``False`` (= caller skips
    the build rather than crashing on a permission / quota issue).
    """
    lock_path = persist_dir / ".build.lock"
    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield False
        return

    def _take_atomic() -> bool:
        try:
            fd = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(
                fd,
                json.dumps({"pid": os.getpid(), "ts": time.time()}).encode(),
            )
        finally:
            os.close(fd)
        return True

    took = _take_atomic()
    if not took:
        # Existing lock — see if the holder is alive.
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            holder_pid = int(data.get("pid", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            holder_pid = 0
        if holder_pid and _pid_alive(holder_pid):
            yield False
            return
        # Stale lock — reap and retry exactly once.
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            yield False
            return
        if not _take_atomic():
            yield False
            return

    try:
        yield True
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Returns 0.0 when either vector is the zero vector (= no direction
    defined).  Robust to floating-point drift; the math.sqrt of a
    negative-by-rounding dot square is clamped to 0.0.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom <= 0.0:
        return 0.0
    return dot / denom


def compute_catalog_hash(items: list[Mapping[str, Any]]) -> str:
    """Snapshot hash over the qualified_name set.

    Stable to ordering, since the items list is sorted before hashing.
    Used as the rebuild trigger: same hash → no-op build.  Phase 2
    step 2 may include short_description in the hash so description
    changes also trigger re-embedding, but step 1 uses the minimal
    qualified_name-set hash for simplicity.
    """
    names = sorted(
        str(it.get("qualified_name", ""))
        for it in items
        if it.get("qualified_name")
    )
    joined = "\n".join(names)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class ActionEmbeddingIndex:
    """In-memory + SQLite-WAL semantic index over action catalog items.

    Holds one embedding vector per qualified_name.  The query path
    embeds the query string once and ranks all stored vectors by
    cosine similarity, returning the top-K items.

    Production wiring (= Phase 2 step 2):
      - One instance per Session (= router-scoped).
      - RouterLoop bootstraps an async ``build()`` task on first turn
        when ``action_retrieval.embedding_class`` is configured.
      - ``search_actions`` handler delegates to ``query()`` when
        ``is_ready()`` returns True; otherwise returns an empty result.
      - ``persist_dir`` points to ``.reyn/action_index/``; pass None
        to stay fully in-memory (= Phase 2 step 1 behaviour, tests).
    """

    def __init__(self, persist_dir: Path | None = None) -> None:
        self._vectors: dict[str, list[float]] = {}
        self._items: dict[str, dict[str, Any]] = {}
        self._catalog_hash: str | None = None
        self._model_class: str | None = None  # FP-0043 Component E: class-swap detection
        self._build_lock = asyncio.Lock()
        self._building = False
        self._persist_dir = persist_dir

    # ── SQLite persistence helpers (Phase 2 step 2) ───────────────────────

    @property
    def db_path(self) -> Path | None:
        if self._persist_dir is None:
            return None
        return self._persist_dir / "index.db"

    def _try_load_from_disk(
        self, expected_hash: str, expected_model_class: str,
    ) -> bool:
        """Load vectors from SQLite if BOTH catalog hash AND model class match.

        Returns True and populates ``_vectors`` / ``_items`` /
        ``_catalog_hash`` / ``_model_class`` when the disk state is
        fresh against the supplied expectations. Returns False (without
        mutating state) when the DB is absent, unreadable, or has a
        different catalog hash or a different model class.

        FP-0043 Component E: model class is checked alongside catalog
        hash so a class swap (= operator edits
        ``action_retrieval.embedding_class`` between sessions, or two
        sessions configured against different classes share the cache
        dir) invalidates the cache. Otherwise vectors from a different
        embedding model would be returned by query() → garbage results.

        Caller MUST hold ``_build_lock``.
        """
        db_path = self.db_path
        if db_path is None or not db_path.exists():
            return False
        try:
            import sqlite3
            con = sqlite3.connect(str(db_path))
            con.execute("PRAGMA journal_mode=WAL")
            try:
                rows = con.execute(
                    "SELECT key, value FROM meta"
                ).fetchall()
                meta = {k: v for k, v in rows}
                if meta.get("catalog_hash") != expected_hash:
                    return False
                if meta.get("model_class") != expected_model_class:
                    return False
                vec_rows = con.execute(
                    "SELECT qualified_name, item_json, vector_blob FROM vectors"
                ).fetchall()
            finally:
                con.close()
            vectors: dict[str, list[float]] = {}
            items: dict[str, dict[str, Any]] = {}
            for qn, item_json, vec_blob in vec_rows:
                n = len(vec_blob) // 8
                vec = list(struct.unpack(f"{n}d", vec_blob))
                vectors[qn] = vec
                items[qn] = json.loads(item_json)
            self._vectors = vectors
            self._items = items
            self._catalog_hash = expected_hash
            self._model_class = expected_model_class
            return True
        except Exception:
            return False

    def _save_to_disk(self) -> None:
        """Write current in-memory state to SQLite (write-through cache).

        No-op when ``persist_dir`` is None or no catalog hash is recorded.
        Persistence failures are swallowed — in-memory state is always
        authoritative; a failed write is retried on the next build.

        FP-0043 Component E: ``model_class`` is persisted alongside
        ``catalog_hash`` so a future load can detect class-swap
        invalidation. The two keys are co-written in the same
        transaction; the new vectors are also written in the same
        transaction so an interrupted save can't leave the meta keys
        pointing at the previous build's vectors.

        Caller MUST hold ``_build_lock``.
        """
        db_path = self.db_path
        if db_path is None or self._catalog_hash is None:
            return
        try:
            import sqlite3
            db_path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(db_path))
            con.execute("PRAGMA journal_mode=WAL")
            try:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                con.execute(
                    "CREATE TABLE IF NOT EXISTS vectors "
                    "(qualified_name TEXT PRIMARY KEY, "
                    "item_json TEXT NOT NULL, vector_blob BLOB NOT NULL)"
                )
                con.execute("DELETE FROM vectors")
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('catalog_hash', ?)",
                    (self._catalog_hash,),
                )
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('model_class', ?)",
                    (self._model_class or "",),
                )
                rows_to_insert = []
                for qn, vec in self._vectors.items():
                    blob = struct.pack(f"{len(vec)}d", *vec)
                    item_j = json.dumps(self._items[qn], ensure_ascii=False)
                    rows_to_insert.append((qn, item_j, blob))
                con.executemany(
                    "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                    rows_to_insert,
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass  # disk failure must not crash the caller

    def is_ready(self) -> bool:
        """Return True iff the index has a completed build available.

        Used by ``search_actions`` handler visibility gating (§D14) and
        by ``build_tools`` to decide whether to expose the wrapper to
        the LLM at all (= Phase 2 step 2 will surface this via the
        RouterCallerState; step 1 only gates the handler response).
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
        ``build()`` call. Exposed as a public read-only property so
        callers + tests can observe the binding without reaching into
        the underscored attribute.
        """
        return self._model_class

    def size(self) -> int:
        """Return the number of indexed items (= vectors stored)."""
        return len(self._vectors)

    async def build(
        self,
        items: list[Mapping[str, Any]],
        provider: "EmbeddingProvider",
        model_class: str,
    ) -> None:
        """Embed each item and store the vector keyed by qualified_name.

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
             (= the existing "different hash triggers rebuild" semantics
             from Phase 2 step 2 are preserved as a strict subset)

        Cross-process build coordination: when ``persist_dir`` is set,
        a non-blocking advisory file lock (= ``.build.lock`` marker
        carrying ``{pid, ts}``) coordinates concurrent builds across
        OS processes. If another live process is mid-build, this call
        falls back to the disk state without invoking the embedding
        provider — preventing duplicate API-cost / duplicate
        sentence-transformers model loads. Single-process callers
        bypass the file lock when ``persist_dir`` is None.
        """
        async with self._build_lock:
            new_hash = compute_catalog_hash(list(items))
            if (
                new_hash == self._catalog_hash
                and self._model_class == model_class
            ):
                return  # idempotent (in-memory match on BOTH axes)

            # Phase 2 step 2 + Component E: try loading from disk first.
            # The disk check honours the same (catalog_hash, model_class)
            # pair, so a class-swap with a fresh catalog hash will load
            # cleanly when that combination was persisted previously
            # (= e.g. user toggles between local-mini and standard).
            if self._try_load_from_disk(new_hash, model_class):
                return  # cache hit — skip embed call

            # Cross-process advisory lock (= take-or-skip). When another
            # live process holds the lock, fall back to whatever's on
            # disk and avoid duplicate embed calls. We accept that the
            # fallback may be a stale state — the in-flight build will
            # publish its result, and the next build() call on this
            # instance observes it via _try_load_from_disk.
            persist_dir = self._persist_dir
            if persist_dir is not None:
                file_lock_cm = _try_acquire_build_lock(persist_dir)
            else:
                # No persist_dir → no cross-process coordination needed
                # (= test / fully-in-memory path). Use a trivial
                # always-acquired context to keep the control flow flat.
                file_lock_cm = contextlib.nullcontext(True)

            with file_lock_cm as got_lock:
                if not got_lock:
                    # Another process is building. We can't safely block
                    # the event loop on a sync lock; surface our current
                    # state (likely empty or stale) and let the next
                    # call observe the in-progress process's result.
                    return

                # Re-check disk under the lock — another process may
                # have completed between our pre-lock check and now.
                if self._try_load_from_disk(new_hash, model_class):
                    return

                # Filter out items missing qualified_name once; embed
                # each remaining one. Keep the items list ordered by
                # sorted qualified_name for determinism in tests.
                valid_items = sorted(
                    (dict(it) for it in items if it.get("qualified_name")),
                    key=lambda it: str(it["qualified_name"]),
                )
                if not valid_items:
                    # Empty catalog — record the empty hash and persist.
                    self._vectors = {}
                    self._items = {}
                    self._catalog_hash = new_hash
                    self._model_class = model_class
                    self._save_to_disk()
                    return

                texts = [
                    f"{it['qualified_name']}: {it.get('short_description', '')}"
                    for it in valid_items
                ]

                self._building = True
                _built_ok = False
                try:
                    result = await provider.embed(texts, model_class)
                    vectors = list(result["vectors"])
                    if len(vectors) != len(valid_items):
                        # Provider returned a mismatched count — refuse
                        # the partial result so we don't end up with a
                        # corrupt half-populated index. The catalog
                        # hash is NOT updated so the next build attempt
                        # retries.
                        raise RuntimeError(
                            f"EmbeddingProvider returned {len(vectors)} "
                            f"vectors for {len(valid_items)} items; "
                            f"refusing partial build"
                        )
                    self._vectors = {
                        str(it["qualified_name"]): list(v)
                        for it, v in zip(valid_items, vectors)
                    }
                    self._items = {
                        str(it["qualified_name"]): it
                        for it in valid_items
                    }
                    self._catalog_hash = new_hash
                    self._model_class = model_class
                    _built_ok = True
                finally:
                    self._building = False
                if _built_ok:
                    self._save_to_disk()

    async def query(
        self,
        query_text: str,
        provider: "EmbeddingProvider",
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
        """
        if not self.is_ready():
            return []
        if not query_text or not query_text.strip():
            return []
        if top_k <= 0:
            return []

        # Embed the query once.
        query_result = await provider.embed([query_text], model_class)
        query_vectors = list(query_result["vectors"])
        if not query_vectors:
            return []
        query_vec = query_vectors[0]

        scored: list[tuple[str, float]] = [
            (qn, _cosine_similarity(query_vec, vec))
            for qn, vec in self._vectors.items()
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)

        out: list[dict[str, Any]] = []
        for qn, score in scored[:top_k]:
            item = dict(self._items[qn])
            item["score"] = score
            out.append(item)
        return out


__all__ = [
    "ActionEmbeddingIndex",
    "compute_catalog_hash",
]
