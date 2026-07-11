"""Tier 2: FP-0057 Phase 0 — action/doc index consolidation onto IndexBackend.

Covers the two acceptance properties for #2843 Phase 0 (folding
``ActionEmbeddingIndex`` onto the pluggable ``IndexBackend``, unifying
cosine math / advisory-lock / dedup with doc-RAG's ``SqliteIndexBackend``):

  1. **Capability seam** — ``IndexBackend`` declares an
     ``existing_hashes_capable`` flag; ``SqliteIndexBackend`` is True.
     This is the in-core-backend pluggability seam (a future alternate
     in-core backend that can't answer ``existing_hashes`` cheaply
     declares False so callers fall back to a full-replace write).

  2. **NON-REGRESSION GATE (co-vet #2, load-bearing)** — ``search_actions``
     is live and used in real runs. Swapping the hand-rolled ``math.sqrt``
     cosine loop for the unified backend's numpy cosine must NOT change
     ranking: same items, same order, stable tie-break, for the same
     query over the same catalog.

     The pre-consolidation cosine algorithm no longer exists in
     production code (it was deleted as part of the fold — see
     ``reyn.tools.action_index``'s module docstring), so this test
     reimplements it verbatim as a frozen oracle and compares its
     ranking against the real, current ``ActionEmbeddingIndex.query()``
     (which now rides ``SqliteIndexBackend``). Vectors are crafted
     (unit vectors with an explicit, well-separated cosine-to-query
     value per item — including one deliberate EXACT tie) rather than
     randomly generated, so the pin is float-precision-proof: ranking
     order is decided by construction, not by incidental separation
     that float32 storage could perturb.

No mocks. Real ``ActionEmbeddingIndex`` + real ``SqliteIndexBackend``
(via ``workspace_root=tmp_path``) + a deterministic fake
``EmbeddingProvider``.
"""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

from reyn.data.index.backends.sqlite import SqliteIndexBackend
from reyn.tools.action_index import ActionEmbeddingIndex

MODEL_CLASS = "standard"
QUERY_TEXT = "find the target action"
_DIM = 8


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── 1. Capability seam ───────────────────────────────────────────────────


def test_sqlite_backend_declares_existing_hashes_capable(tmp_path: Path) -> None:
    """Tier 2: SqliteIndexBackend declares the existing_hashes capability flag.

    The flag is the in-core-backend pluggability seam — a future alternate
    in-core backend that can't answer "which content_hashes exist" cheaply
    would declare False; SqliteIndexBackend answers it directly off the
    local DB, so it's True.
    """
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    assert backend.existing_hashes_capable is True


# ── 2. Non-regression gate: ranking pre/post cosine-impl unification ────


def _pre_consolidation_cosine(a: list[float], b: list[float]) -> float:
    """Frozen oracle — VERBATIM copy of the deleted hand-rolled cosine loop
    that used to live in ``reyn.tools.action_index._cosine_similarity``
    (pre-#2843). Deliberately duplicated here (not imported — the
    production copy no longer exists) so this test pins behaviour
    independent of the current implementation."""
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


def _pre_consolidation_query(
    vectors: dict[str, list[float]], query_vec: list[float], top_k: int,
) -> list[tuple[str, float]]:
    """Frozen oracle — VERBATIM copy of the deleted
    ``ActionEmbeddingIndex.query`` ranking loop: score every stored vector,
    stable-sort descending (Python's ``list.sort(reverse=True)`` is
    stable — ties keep the dict's insertion order), take top_k."""
    scored: list[tuple[str, float]] = [
        (qn, _pre_consolidation_cosine(query_vec, vec))
        for qn, vec in vectors.items()
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]


def _unit_vec_at_cosine(target_cosine: float) -> list[float]:
    """A unit vector in R^_DIM whose cosine similarity to [1,0,...,0] is
    EXACTLY ``target_cosine`` (by construction: v = [c, sqrt(1-c^2), 0...]).
    This makes the oracle's expected ranking a pure function of
    ``target_cosine`` — no incidental float noise to reason about."""
    c = target_cosine
    rest = math.sqrt(max(0.0, 1.0 - c * c))
    return [c, rest] + [0.0] * (_DIM - 2)


def _catalog_fixture() -> list[dict[str, Any]]:
    """~40 items across 4 categories, each carrying a crafted cosine-to-
    query affinity. Includes one deliberate EXACT tie (see items 'k' and
    'k_twin') to pin the stable tie-break requirement."""
    categories = ["file", "web", "mcp", "pipeline"]
    items: list[dict[str, Any]] = []
    n = 40
    for i in range(n):
        cat = categories[i % len(categories)]
        qn = f"{cat}__action_{i:03d}"
        # Well-separated affinities spanning [-0.9, 0.9], strictly
        # decreasing with i (spacing 0.045 — far above float32 epsilon).
        affinity = 0.9 - i * 0.045
        items.append({
            "qualified_name": qn,
            "short_description": f"Action number {i} in category {cat}",
            "_affinity": affinity,
        })
    # Deliberate exact tie: two items share the SAME affinity as item 10,
    # to pin stable tie-break (insertion order = qualified_name-sorted
    # order, since build() sorts items by qualified_name before embedding).
    tie_affinity = items[10]["_affinity"]
    twin_qn = "pipeline__action_010_twin"
    items.append({
        "qualified_name": twin_qn,
        "short_description": "Tied-score twin of action_010",
        "_affinity": tie_affinity,
    })
    return items


class _CraftedVectorProvider:
    """Deterministic EmbeddingProvider: returns the crafted unit vector for
    each catalog item's qualified_name (looked up via the embed text, which
    ``ActionEmbeddingIndex`` always builds as ``f"{qn}: {desc}"``), and the
    query vector [1, 0, ..., 0] for the query text."""

    def __init__(self, affinity_by_qn: dict[str, float]) -> None:
        self._affinity_by_qn = affinity_by_qn
        self.embed_calls = 0

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.embed_calls += 1
        vectors: list[list[float]] = []
        for t in texts:
            if t == QUERY_TEXT:
                vectors.append([1.0] + [0.0] * (_DIM - 1))
                continue
            qn = t.split(":", 1)[0]
            affinity = self._affinity_by_qn[qn]
            vectors.append(_unit_vec_at_cosine(affinity))
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


def test_topk_ranking_identical_to_pre_consolidation_oracle(
    tmp_path: Path,
) -> None:
    """Tier 2: co-vet #2 non-regression gate.

    Builds the real (post-consolidation) ActionEmbeddingIndex over a
    crafted catalog, queries it, and asserts the top-K (items, order,
    scores) EXACTLY match the frozen pre-consolidation hand-rolled-cosine
    oracle run over the same vectors — including the deliberate tied
    pair's tie-break order.
    """
    items = _catalog_fixture()
    tied_qn_a = items[10]["qualified_name"]  # "mcp__action_010"
    tied_qn_b = items[-1]["qualified_name"]  # the deliberate twin, appended last
    affinity_by_qn = {it["qualified_name"]: it["_affinity"] for it in items}
    catalog_items = [
        {"qualified_name": it["qualified_name"],
         "short_description": it["short_description"]}
        for it in items
    ]

    # ── oracle: run the frozen pre-consolidation algorithm directly ──
    # over the same vectors build() would have produced (sorted by
    # qualified_name, matching the pre-consolidation insertion order).
    sorted_qns = sorted(affinity_by_qn)
    oracle_vectors = {
        qn: _unit_vec_at_cosine(affinity_by_qn[qn]) for qn in sorted_qns
    }
    query_vec = [1.0] + [0.0] * (_DIM - 1)
    # top_k=12 deliberately includes items 0-9 (strictly decreasing
    # affinity) PLUS the tied pair (item 10 + its twin, both at the same
    # affinity) so the tie-break assertion below has something to pin.
    top_k = 12
    oracle_ranked = _pre_consolidation_query(oracle_vectors, query_vec, top_k)
    oracle_qns = [qn for qn, _score in oracle_ranked]
    oracle_scores = [score for _qn, score in oracle_ranked]

    # ── production: real ActionEmbeddingIndex riding SqliteIndexBackend ──
    provider = _CraftedVectorProvider(affinity_by_qn)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    _run(idx.build(catalog_items, provider, MODEL_CLASS))
    results = _run(idx.query(QUERY_TEXT, provider, MODEL_CLASS, top_k=top_k))

    production_qns = [r["qualified_name"] for r in results]
    production_scores = [r["score"] for r in results]

    assert production_qns == oracle_qns, (
        f"top-{top_k} ranking diverged from the pre-consolidation oracle.\n"
        f"oracle:     {oracle_qns}\n"
        f"production: {production_qns}"
    )
    for prod_score, oracle_score in zip(production_scores, oracle_scores):
        assert abs(prod_score - oracle_score) < 1e-4, (
            f"score diverged beyond float32-storage tolerance: "
            f"production={prod_score} oracle={oracle_score}"
        )

    # The deliberate tie (item 10 vs its twin) resolved in
    # qualified_name-sorted order on BOTH sides — pin the tie-break
    # explicitly, not just as an incidental consequence of the full-list
    # comparison above.
    tie_qns = {tied_qn_a, tied_qn_b}
    expected_tie_order = sorted(tie_qns)
    oracle_tie_order = [qn for qn in oracle_qns if qn in tie_qns]
    production_tie_order = [qn for qn in production_qns if qn in tie_qns]
    assert oracle_tie_order == production_tie_order == expected_tie_order, (
        "stable tie-break order must match the pre-consolidation "
        f"contract; oracle={oracle_tie_order} production={production_tie_order}"
    )
