"""ActionEmbeddingIndex — in-memory semantic index for FP-0034 search_actions.

FP-0034 §D13 / §D15 spec — Phase 2 step 1 (in-memory only; SQLite-WAL
persistence per ADR-0033 lands in step 2).

Lifecycle:
  1. Construction: empty index, ``is_ready() == False``.
  2. ``await build(items, provider, model_class)`` — embeds each item's
     ``"{qualified_name}: {short_description}"`` text via the
     ``EmbeddingProvider``, stores the vectors keyed by qualified_name,
     and records a catalog snapshot hash. On completion ``is_ready()``
     returns True.
  3. ``await query(text, provider, model_class, top_k=10)`` — embeds
     the query once, ranks all stored vectors by cosine similarity,
     and returns the top-K items with their ``score``. When the index
     is not ready, returns ``[]`` so callers (= ``search_actions``
     handler) gracefully degrade instead of crashing.

Catalog hash semantics:
  - Hash is over the SORTED tuple of qualified_names.
  - When ``build()`` is called with the same hash, it is a no-op
    (= idempotent reload guard).
  - Different hash → rebuild (Phase 2 step 2 will diff for incremental
    updates; step 1 rebuilds the whole vector set).

Concurrency:
  - ``_build_lock`` serialises concurrent ``build()`` calls; the second
    call awaits the first.  No partial-state visibility while building
    (``is_ready()`` stays False until the in-progress build completes).
  - ``query()`` reads ``_vectors`` / ``_items`` without locking — Python's
    GIL gives single-statement atomic dict reads; concurrent ``build()``
    is bounded by ``is_ready()`` being False so production callers
    skip ``query()`` while a build is in flight.

Not yet implemented (= Phase 2 step 2+):
  - SQLite-WAL persistence under ``.reyn/action_index/``
  - Stale-detection via catalog snapshot on disk + content hash for skill files
  - Incremental rebuild (diff between catalog hashes)
  - ``ActionUsageTracker`` for hot-list ranking
"""
from __future__ import annotations

import asyncio
import hashlib
import math
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from reyn.embedding.provider import EmbeddingProvider


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
    """In-memory semantic index over action catalog items.

    Holds one embedding vector per qualified_name.  The query path
    embeds the query string once and ranks all stored vectors by
    cosine similarity, returning the top-K items.

    Production wiring (= Phase 2 step 1):
      - One instance per ChatSession (= router-scoped).
      - RouterLoop bootstraps an async ``build()`` task on first turn
        when ``action_retrieval.embedding_class`` is configured.
      - ``search_actions`` handler delegates to ``query()`` when
        ``is_ready()`` returns True; otherwise returns an empty result.
    """

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}
        self._items: dict[str, dict[str, Any]] = {}
        self._catalog_hash: str | None = None
        self._build_lock = asyncio.Lock()
        self._building = False

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

        Idempotent: when the catalog hash matches the current state,
        returns immediately without re-embedding.  Different hash
        triggers a full rebuild (Phase 2 step 2 will diff).
        """
        async with self._build_lock:
            new_hash = compute_catalog_hash(list(items))
            if new_hash == self._catalog_hash:
                return  # idempotent

            # Filter out items missing qualified_name once; embed each
            # remaining one.  Keep the items list ordered by sorted
            # qualified_name for determinism in tests.
            valid_items = sorted(
                (dict(it) for it in items if it.get("qualified_name")),
                key=lambda it: str(it["qualified_name"]),
            )
            if not valid_items:
                # Empty catalog — record the empty hash and return.
                self._vectors = {}
                self._items = {}
                self._catalog_hash = new_hash
                return

            texts = [
                f"{it['qualified_name']}: {it.get('short_description', '')}"
                for it in valid_items
            ]

            self._building = True
            try:
                result = await provider.embed(texts, model_class)
                vectors = list(result["vectors"])
                if len(vectors) != len(valid_items):
                    # Provider returned a mismatched count — refuse the
                    # partial result so we don't end up with a corrupt
                    # half-populated index.  The catalog hash is NOT
                    # updated so the next build attempt retries.
                    raise RuntimeError(
                        f"EmbeddingProvider returned {len(vectors)} vectors "
                        f"for {len(valid_items)} items; refusing partial build"
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
            finally:
                self._building = False

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
