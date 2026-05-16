"""Tier 2: FP-0034 Phase 2 step 1 — ActionEmbeddingIndex.

Verifies the in-memory action embedding index that powers
``search_actions``.  Uses a plain ``FakeEmbeddingProvider`` (= no
mocks, no LLMReplay) so the tests pin the index contract independently
of any real embedding backend.

Coverage:
  - is_ready() lifecycle (= False → True after build)
  - catalog_hash idempotence (= same hash = no-op rebuild)
  - hash sensitivity to qualified_name set changes
  - query top-K ranking via cosine similarity
  - empty / whitespace query degrades to []
  - not-ready query degrades to []
  - top_k <= 0 degrades to []
  - mismatched provider vector count refuses the partial build
  - empty catalog handled cleanly (empty hash recorded)

No mocks. The FakeEmbeddingProvider is a plain async class
implementing the EmbeddingProvider Protocol's ``embed`` method.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.tools.action_index import (
    ActionEmbeddingIndex,
    compute_catalog_hash,
)


# ── Fake EmbeddingProvider ────────────────────────────────────────────────


class _FakeEmbeddingProvider:
    """In-test EmbeddingProvider — deterministic canned vectors.

    Encodes the text as a 4-dimension vector where each component
    derives from a hash of the input + a position-specific seed.
    Gives distinct vectors per distinct text so cosine similarity
    rankings are non-degenerate.
    """
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.calls.append((tuple(texts), model))
        vectors: list[list[float]] = []
        for t in texts:
            # 4-dim vector seeded by per-component hash buckets so
            # distinct strings produce distinct vectors.
            v = [
                float((hash((t, i)) % 1000) / 1000.0) for i in range(4)
            ]
            vectors.append(v)
        return {"vectors": vectors, "model": model, "total_tokens": len(texts)}


class _DegenerateFakeProvider:
    """Returns the WRONG number of vectors — for partial-build refusal test."""
    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        return {
            "vectors": [[1.0, 0.0]],  # always 1 vector regardless of input
            "model": model,
            "total_tokens": 1,
        }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── 1. is_ready() lifecycle ───────────────────────────────────────────────


def test_initial_state_not_ready() -> None:
    """Tier 2: fresh index reports not ready."""
    idx = ActionEmbeddingIndex()
    assert idx.is_ready() is False
    assert idx.size() == 0
    assert idx.catalog_hash() is None


def test_build_then_ready() -> None:
    """Tier 2: build() transitions index to ready."""
    idx = ActionEmbeddingIndex()
    _run(idx.build(
        [{"qualified_name": "skill__a", "short_description": "A"}],
        _FakeEmbeddingProvider(),
        "standard",
    ))
    assert idx.is_ready() is True
    assert idx.size() == 1
    assert idx.catalog_hash() is not None


# ── 2. catalog_hash semantics ─────────────────────────────────────────────


def test_catalog_hash_stable_to_order() -> None:
    """Tier 2: catalog hash is order-independent (sorted qualified_names)."""
    a = compute_catalog_hash([
        {"qualified_name": "skill__foo"},
        {"qualified_name": "skill__bar"},
    ])
    b = compute_catalog_hash([
        {"qualified_name": "skill__bar"},
        {"qualified_name": "skill__foo"},
    ])
    assert a == b


def test_catalog_hash_changes_when_names_change() -> None:
    """Tier 2: adding a qualified_name changes the hash."""
    a = compute_catalog_hash([{"qualified_name": "skill__foo"}])
    b = compute_catalog_hash([
        {"qualified_name": "skill__foo"},
        {"qualified_name": "skill__bar"},
    ])
    assert a != b


def test_idempotent_rebuild_with_same_catalog() -> None:
    """Tier 2: second build() with same catalog is a no-op (no re-embed)."""
    items = [
        {"qualified_name": "skill__a", "short_description": "A"},
        {"qualified_name": "skill__b", "short_description": "B"},
    ]
    provider = _FakeEmbeddingProvider()
    idx = ActionEmbeddingIndex()
    _run(idx.build(items, provider, "standard"))
    first_call_count = len(provider.calls)
    _run(idx.build(items, provider, "standard"))
    assert len(provider.calls) == first_call_count, (
        "Second build with identical catalog must not re-embed; "
        f"provider.calls grew from {first_call_count} to {len(provider.calls)}"
    )


def test_rebuild_when_catalog_changes() -> None:
    """Tier 2: changing the qualified_name set triggers a fresh build."""
    provider = _FakeEmbeddingProvider()
    idx = ActionEmbeddingIndex()
    _run(idx.build(
        [{"qualified_name": "skill__a", "short_description": "A"}],
        provider, "standard",
    ))
    first_call_count = len(provider.calls)
    _run(idx.build(
        [
            {"qualified_name": "skill__a", "short_description": "A"},
            {"qualified_name": "skill__b", "short_description": "B"},
        ],
        provider, "standard",
    ))
    assert len(provider.calls) > first_call_count
    assert idx.size() == 2


# ── 3. query() — top-K ranking ────────────────────────────────────────────


def test_query_returns_top_k_items() -> None:
    """Tier 2: query returns top_k results sorted by score descending."""
    items = [
        {"qualified_name": f"skill__item_{i}", "short_description": f"Item {i}"}
        for i in range(5)
    ]
    idx = ActionEmbeddingIndex()
    _run(idx.build(items, _FakeEmbeddingProvider(), "standard"))
    results = _run(idx.query("query for item 0", _FakeEmbeddingProvider(),
                              "standard", top_k=3))
    assert len(results) == 3
    # Scores must be monotonically non-increasing.
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    # Each result has the original fields + score.
    for r in results:
        assert "qualified_name" in r
        assert "short_description" in r
        assert "score" in r
        assert -1.0 <= r["score"] <= 1.0


def test_query_top_k_larger_than_catalog_returns_all() -> None:
    """Tier 2: top_k > catalog size returns the full catalog."""
    items = [
        {"qualified_name": "skill__only", "short_description": "Only one"},
    ]
    idx = ActionEmbeddingIndex()
    _run(idx.build(items, _FakeEmbeddingProvider(), "standard"))
    results = _run(idx.query("anything", _FakeEmbeddingProvider(),
                              "standard", top_k=10))
    assert len(results) == 1


# ── 4. Graceful degradation ───────────────────────────────────────────────


def test_query_not_ready_returns_empty() -> None:
    """Tier 2: querying before build() returns []."""
    idx = ActionEmbeddingIndex()
    assert _run(idx.query("anything", _FakeEmbeddingProvider(), "standard")) == []


def test_query_empty_string_returns_empty() -> None:
    """Tier 2: empty query returns [] (skip wasteful embedding call)."""
    idx = ActionEmbeddingIndex()
    _run(idx.build(
        [{"qualified_name": "skill__a", "short_description": "A"}],
        _FakeEmbeddingProvider(), "standard",
    ))
    assert _run(idx.query("", _FakeEmbeddingProvider(), "standard")) == []
    assert _run(idx.query("   ", _FakeEmbeddingProvider(), "standard")) == []


def test_query_zero_top_k_returns_empty() -> None:
    """Tier 2: top_k <= 0 returns []."""
    idx = ActionEmbeddingIndex()
    _run(idx.build(
        [{"qualified_name": "skill__a", "short_description": "A"}],
        _FakeEmbeddingProvider(), "standard",
    ))
    assert _run(idx.query("q", _FakeEmbeddingProvider(),
                           "standard", top_k=0)) == []
    assert _run(idx.query("q", _FakeEmbeddingProvider(),
                           "standard", top_k=-1)) == []


# ── 5. Edge cases ─────────────────────────────────────────────────────────


def test_empty_catalog_records_hash_no_vectors() -> None:
    """Tier 2: build() with empty items still records the empty hash."""
    idx = ActionEmbeddingIndex()
    _run(idx.build([], _FakeEmbeddingProvider(), "standard"))
    assert idx.is_ready() is True
    assert idx.size() == 0
    assert idx.catalog_hash() is not None


def test_items_without_qualified_name_dropped() -> None:
    """Tier 2: items missing qualified_name are silently skipped."""
    idx = ActionEmbeddingIndex()
    _run(idx.build(
        [
            {"qualified_name": "skill__valid", "short_description": "Valid"},
            {"short_description": "No qualified_name field"},  # dropped
            {"qualified_name": "", "short_description": "Empty name"},  # dropped
        ],
        _FakeEmbeddingProvider(),
        "standard",
    ))
    assert idx.size() == 1


def test_mismatched_vector_count_refuses_partial_build() -> None:
    """Tier 2: provider returning wrong vector count → RuntimeError, no state.

    Refuses partial state so we don't end up with a corrupt half-populated
    index.  The catalog hash stays None so the next build retries.
    """
    idx = ActionEmbeddingIndex()
    with pytest.raises(RuntimeError, match="refusing partial build"):
        _run(idx.build(
            [
                {"qualified_name": "skill__a"},
                {"qualified_name": "skill__b"},  # 2 items but provider returns 1
            ],
            _DegenerateFakeProvider(),
            "standard",
        ))
    assert idx.is_ready() is False
    assert idx.size() == 0
    assert idx.catalog_hash() is None


# ── 6. SQLite persistence (Phase 2 step 2) ───────────────────────────────


def test_persist_dir_build_creates_db_file(tmp_path: "Path") -> None:
    """Tier 2: build() with persist_dir writes catalog_hash to index.db."""
    from pathlib import Path

    persist_dir = tmp_path / "action_index"
    items = [{"qualified_name": "skill__a", "short_description": "A"}]
    idx = ActionEmbeddingIndex(persist_dir=persist_dir)
    _run(idx.build(items, _FakeEmbeddingProvider(), "standard"))

    db_path = persist_dir / "index.db"
    assert db_path.exists(), "index.db must be created after build()"

    import sqlite3
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key='catalog_hash'"
        ).fetchone()
        assert row is not None
        assert row[0] == idx.catalog_hash()

        rows = con.execute("SELECT qualified_name FROM vectors").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "skill__a"
    finally:
        con.close()


def test_persist_dir_loads_from_disk_skips_embed(tmp_path: "Path") -> None:
    """Tier 2: second index with same persist_dir loads from disk, skips embed.

    Simulates a process restart: a fresh ActionEmbeddingIndex is created
    (empty in-memory state) and build() is called with the same catalog.
    The disk cache hit must prevent any embed call.
    """
    persist_dir = tmp_path / "action_index"
    items = [
        {"qualified_name": "skill__a", "short_description": "A"},
        {"qualified_name": "skill__b", "short_description": "B"},
    ]

    # First process — build + persist
    idx1 = ActionEmbeddingIndex(persist_dir=persist_dir)
    provider1 = _FakeEmbeddingProvider()
    _run(idx1.build(items, provider1, "standard"))
    assert len(provider1.calls) == 1

    # Second process (simulated) — fresh index, same catalog
    idx2 = ActionEmbeddingIndex(persist_dir=persist_dir)
    provider2 = _FakeEmbeddingProvider()
    _run(idx2.build(items, provider2, "standard"))

    assert len(provider2.calls) == 0, (
        "build() must load from disk and skip the embed call on cache hit"
    )
    assert idx2.is_ready() is True
    assert idx2.size() == 2
    assert idx2.catalog_hash() == idx1.catalog_hash()


def test_persist_dir_rebuilds_on_stale_disk_hash(tmp_path: "Path") -> None:
    """Tier 2: changed catalog triggers re-embed and overwrites disk hash."""
    persist_dir = tmp_path / "action_index"
    items_v1 = [{"qualified_name": "skill__a", "short_description": "A"}]
    items_v2 = [
        {"qualified_name": "skill__a", "short_description": "A"},
        {"qualified_name": "skill__b", "short_description": "B"},
    ]

    idx1 = ActionEmbeddingIndex(persist_dir=persist_dir)
    _run(idx1.build(items_v1, _FakeEmbeddingProvider(), "standard"))
    hash_v1 = idx1.catalog_hash()

    # Fresh index — different catalog → must re-embed, not load v1 from disk
    idx2 = ActionEmbeddingIndex(persist_dir=persist_dir)
    provider2 = _FakeEmbeddingProvider()
    _run(idx2.build(items_v2, provider2, "standard"))

    assert len(provider2.calls) == 1, "stale disk hash must trigger re-embed"
    assert idx2.size() == 2
    assert idx2.catalog_hash() != hash_v1

    # Disk must now carry the new hash
    import sqlite3
    con = sqlite3.connect(str(persist_dir / "index.db"))
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key='catalog_hash'"
        ).fetchone()
        assert row is not None
        assert row[0] == idx2.catalog_hash()
    finally:
        con.close()


def test_no_persist_dir_no_file_created(tmp_path: "Path") -> None:
    """Tier 2: without persist_dir, no file is written (pure in-memory mode)."""
    items = [{"qualified_name": "skill__a", "short_description": "A"}]
    idx = ActionEmbeddingIndex(persist_dir=None)
    _run(idx.build(items, _FakeEmbeddingProvider(), "standard"))
    assert idx.is_ready() is True
    assert idx._db_path is None
