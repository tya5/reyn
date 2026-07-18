"""Tier 2b: subsystem invariant -- FP-0063 P2, the two builtin MCP servers
(``reyn.builtin.plugins.rag.scripts.vector_store_server`` / ``chunker_server``),
docs/deep-dives/proposals/0063-builtin-turnkey-user-rag.md.

Pins the five vector-store gates + the chunker size/overlap-is-a-parameter
contract, using REAL ``apsw`` + real ``sqlite-vec`` + real ``chonkie``
instances throughout (``pytest.importorskip`` guards the whole module: the
``builtin-rag`` extra is optional, a base install must not fail collection).
No mocks anywhere -- every assertion drives the actual ``SqliteVecStore`` /
``chunk_text`` call path, per testing policy.

Coverage:
  1. Pre-computed-vector round trip: store -> query -> the SAME vector's
     metadata comes back nearest (gate 1+4).
  2. User-specified sqlite path: the db file lands at the exact path passed
     in, including creating missing parent directories (gate 2).
  3. Metadata-filtered listing returns metadata WITHOUT vectors (gate 5,
     Chroma ``get(where=...)`` shape) -- strip-falsified below (#4).
  4. STRIP-FALSIFICATION: removing the ``list_metadata`` column-restriction
     (simulated by calling the SELECT with ``*`` including the embedding
     join) would leak vector data; the real implementation's dict keys are
     asserted to exclude "vector"/"embedding" -- and a broken variant that
     forgets the restriction is shown RED to prove the assertion is live
     (not vacuously true).
  5. Upsert replaces rather than duplicates (same identity, new vector ->
     row count unchanged, new vector wins the query).
  6. Delete removes (gate 5) and is a no-op for unknown ids.
  7. Top-k + a plain-SQL metadata filter narrows results (gate 3).
  8. Chunker: ``size``/``overlap_ratio`` are real parameters with real
     effect on the output (R4) -- smaller size -> more chunks; higher
     overlap -> larger merged chunk token counts. Defaults hit the
     256-512-token 2026 band.
  9. #2972 -- the four behaviors that moved INTO these servers when the
     ingest pipeline stopped shelling out to python: ``upsert``'s parallel
     items/vectors arrays (incl. the length-mismatch rejection), its
     per-call ``embedding_model``/``parent_context`` stamping, its derived
     (source_path, chunk_index) key, and the chunker's per-chunk
     ``content_hash``/``chunk_index``/``est_tokens``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

apsw = pytest.importorskip(
    "apsw", reason="builtin-rag extra ('pip install reyn[builtin-rag]') not installed",
)
sqlite_vec = pytest.importorskip(
    "sqlite_vec", reason="builtin-rag extra ('pip install reyn[builtin-rag]') not installed",
)
pytest.importorskip(
    "chonkie", reason="builtin-rag extra ('pip install reyn[builtin-rag]') not installed",
)

from reyn.builtin.plugins.rag.scripts.chunker_server import chunk_text  # noqa: E402
from reyn.builtin.plugins.rag.scripts.vector_store_server import (  # noqa: E402
    METADATA_COLUMNS,
    SqliteVecStore,
    VectorDimensionMismatchError,
)

# The model/parent_context every upsert below stamps -- passed per CALL now
# (they are per-batch facts, not per-item ones), so they live here rather than
# in the item dict.
_MODEL = "text-embedding-3-small"
_PARENT = "Introduction"


def _sample_item(**overrides) -> dict:
    """One upsert item. NOTE the absent keys: `id` is DERIVED by the store from
    (source_path, chunk_index) and `embedding_model`/`parent_context` are
    stamped per call -- a caller cannot spell any of the three (#2972)."""
    base = {
        "source_path": "docs/intro.md",
        "source_type": "doc",
        "content_hash": "hash-a",
        "chunk_index": 0,
        "size_tokens": 42,
        "extra": {"lang": "en"},
    }
    base.update(overrides)
    return base


def _id_of(item: dict) -> str:
    """The store's derived key for `item`, spelled by its documented formula
    rather than hardcoded, so these tests assert the ROUND TRIP (what goes in
    comes back out under a stable key) rather than re-pinning the format."""
    return f"{item['source_path']}::{item['chunk_index']}"


def test_precomputed_vector_round_trip(tmp_path: Path) -> None:
    """Tier 2b: Gate 1+4: a caller-supplied vector + full ChunkMetadata shape survive
    store -> query, and the query for that exact vector returns it nearest
    with metadata intact (source_path/source_type/content_hash/
    embedding_model/chunk_index/size_tokens/parent_context/extra)."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        item_a = _sample_item()
        item_b = _sample_item(source_path="docs/other.md", content_hash="hash-b")
        store.upsert([item_a, item_b], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], _MODEL,
                     parent_context=_PARENT)
        results = store.query([1.0, 0.0, 0.0], top_k=2)

    assert results[0]["id"] == _id_of(item_a)
    assert results[0]["distance"] == pytest.approx(0.0, abs=1e-6)
    meta = results[0]["metadata"]
    assert meta["source_path"] == "docs/intro.md"
    assert meta["source_type"] == "doc"
    assert meta["content_hash"] == "hash-a"
    assert meta["embedding_model"] == "text-embedding-3-small"
    assert meta["chunk_index"] == 0
    assert meta["size_tokens"] == 42
    assert meta["parent_context"] == "Introduction"
    assert meta["extra"] == {"lang": "en"}


def test_db_lands_at_user_specified_path_incl_missing_parents(tmp_path: Path) -> None:
    """Tier 2b: Gate 2: the sqlite file is created at the EXACT path passed by the
    caller, including creating a missing parent directory -- mirrors the
    owner's "output file name specified" requirement."""
    db_path = tmp_path / "nested" / "sub" / "my_project.sqlite"
    assert not db_path.parent.exists()
    with SqliteVecStore(str(db_path)) as store:
        store.upsert([_sample_item()], [[1.0]], _MODEL, parent_context=_PARENT)
    assert db_path.is_file()
    # Real sqlite file: openable independently via stdlib sqlite3 (a distinct
    # connection than the one the store held), proving it's a genuine
    # on-disk sqlite db and not merely a placeholder.
    conn = sqlite3.connect(str(db_path))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "reyn_rag_chunks" in tables


def test_list_metadata_excludes_vectors(tmp_path: Path) -> None:
    """Tier 2b: Gate 5: metadata-filtered listing returns metadata WITHOUT vectors."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        item = _sample_item()
        store.upsert([item], [[1.0, 2.0, 3.0]], _MODEL, parent_context=_PARENT)
        listed = store.list_metadata()

    assert [e["id"] for e in listed] == [_id_of(item)]
    entry = listed[0]
    assert "vector" not in entry
    assert "vector" not in entry["metadata"]
    assert "embedding" not in entry["metadata"]
    assert set(entry["metadata"]) == set(METADATA_COLUMNS) | {"extra"}


def test_strip_falsify_list_metadata_would_leak_vectors_if_joined(tmp_path: Path) -> None:
    """Tier 2b: STRIP-FALSIFICATION -- show the RED that would
    result if ``list_metadata`` joined the vector table instead of reading
    only ``reyn_rag_chunks``. We don't monkeypatch production code (no
    mocks/patches allowed) -- instead we reproduce the broken query
    ourselves against the SAME real db/schema the store created, proving
    the real implementation's column restriction is load-bearing rather
    than vacuously true (i.e. the vector data really is sitting right next
    to the metadata columns and a naive listing WOULD surface it)."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        item = _sample_item()
        store.upsert([item], [[1.0, 2.0, 3.0]], _MODEL, parent_context=_PARENT)
        good = store.list_metadata()
        assert "vector" not in good[0]["metadata"]  # GREEN: real implementation

    # The "broken variant": a naive listing that also selects the raw
    # vector blob via the same rowid join the store's own `query()` uses
    # internally. This opens a SEPARATE, independent connection to the
    # SAME on-disk file (public surface only -- no reach into the store's
    # internals) purely to PROVE the vector bytes are retrievable at all
    # in this schema, i.e. that list_metadata's vector-exclusion is a
    # deliberate choice guarding real data, not an accident of the data
    # not being there.
    probe = apsw.Connection(db_path)
    probe.enable_load_extension(True)
    probe.load_extension(sqlite_vec.loadable_path())
    probe.enable_load_extension(False)
    row = probe.execute(
        "SELECT v.embedding FROM reyn_rag_vectors v "
        "JOIN reyn_rag_chunks c ON c.rowid = v.rowid WHERE c.rag_id = ?",
        (_id_of(item),),
    ).fetchone()
    probe.close()
    assert row is not None and row[0] is not None, (
        "RED (expected if this fails): the broken naive-join query returned "
        "no vector bytes, meaning the leak this test guards against isn't "
        "reachable in this schema -- list_metadata's vector-exclusion would "
        "then be vacuous rather than a real gate."
    )


def test_upsert_replaces_not_duplicates(tmp_path: Path) -> None:
    """Tier 2b: same id, new vector+metadata -> no duplicate row is
    created and the NEW vector wins subsequent queries."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        v1 = _sample_item(content_hash="v1")
        v2 = _sample_item(content_hash="v2")
        assert _id_of(v1) == _id_of(v2), "same identity, changed content -- the replace case"
        store.upsert([v1], [[1.0, 0.0]], _MODEL, parent_context=_PARENT)
        store.upsert([v2], [[0.0, 1.0]], _MODEL, parent_context=_PARENT)
        listed = store.list_metadata()
        results = store.query([0.0, 1.0], top_k=5)

    assert [e["id"] for e in listed] == [_id_of(v2)]
    assert listed[0]["metadata"]["content_hash"] == "v2"
    assert [r["id"] for r in results] == [_id_of(v2)]
    assert results[0]["distance"] == pytest.approx(0.0, abs=1e-6)


def test_delete_removes_and_is_noop_for_unknown_id(tmp_path: Path) -> None:
    """Tier 2b: Gate 5: delete removes an existing id; an unknown id is skipped
    (no error, ``deleted`` count reflects only real removals)."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        keep = _sample_item(chunk_index=1)
        drop = _sample_item(chunk_index=0)
        store.upsert([drop, keep], [[1.0], [2.0]], _MODEL, parent_context=_PARENT)
        deleted = store.delete([_id_of(drop), "does-not-exist"])
        remaining = store.list_metadata()

    assert deleted == 1
    assert [entry["id"] for entry in remaining] == [_id_of(keep)]


def test_topk_with_metadata_filter(tmp_path: Path) -> None:
    """Tier 2b: Gate 3: top-k + a plain-SQL equality metadata filter narrows the
    candidate set even when an unfiltered top-k would include a nearer
    non-matching row."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        near_wrong = _sample_item(source_path="wrong.md")
        far_right = _sample_item(source_path="right.md")
        store.upsert([near_wrong, far_right], [[1.0, 0.0], [0.0, 1.0]], _MODEL,
                     parent_context=_PARENT)
        unfiltered = store.query([1.0, 0.0], top_k=1)
        filtered = store.query([1.0, 0.0], top_k=1, filters={"source_path": "right.md"})

    assert unfiltered[0]["id"] == _id_of(near_wrong)
    assert filtered[0]["id"] == _id_of(far_right)


def test_dimension_mismatch_raises(tmp_path: Path) -> None:
    """Tier 2b: C4 guard -- a store's vector dimension is fixed at first upsert; a
    later vector of a different dimension is rejected rather than silently
    corrupting the vec0 table."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        store.upsert([_sample_item()], [[1.0, 2.0, 3.0]], _MODEL, parent_context=_PARENT)
        with pytest.raises(VectorDimensionMismatchError):
            store.upsert([_sample_item(chunk_index=1)], [[1.0, 2.0]], _MODEL,
                         parent_context=_PARENT)


def test_upsert_rejects_mismatched_items_and_vectors(tmp_path: Path) -> None:
    """Tier 2b: #2972 -- items/vectors are parallel arrays, so a length
    mismatch is rejected rather than silently pairing the wrong vector with
    the wrong chunk (or dropping the tail).

    This guard used to live in the pipeline's python shell-out; it has to
    survive the move into `upsert`, because the failure it prevents is
    silent and corrupting: every chunk would keep its own metadata while
    carrying someone else's embedding, and no query would ever look wrong
    enough to notice."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        with pytest.raises(ValueError):
            store.upsert(
                [_sample_item(chunk_index=0), _sample_item(chunk_index=1)],
                [[1.0, 0.0]],  # one vector for two items
                _MODEL,
                parent_context=_PARENT,
            )
        assert store.list_metadata() == [], "a rejected upsert must store nothing"


def test_upsert_stamps_the_model_and_parent_context_it_is_given(tmp_path: Path) -> None:
    """Tier 2b: #2972/C4 -- embedding_model and parent_context are stamped
    from the CALL's arguments onto every row.

    They are per-batch facts (one embed call produced these vectors under one
    model), which is why they are call arguments rather than per-item fields
    the caller could vary or forget. Uses values distinct from the item's own
    fields so the stamp is attributable to the argument."""
    db_path = str(tmp_path / "rag.sqlite")
    with SqliteVecStore(db_path) as store:
        store.upsert(
            [_sample_item(chunk_index=0), _sample_item(chunk_index=1)],
            [[1.0, 0.0], [0.0, 1.0]],
            "resolved/model-xyz",
            parent_context="/ingest/root",
        )
        listed = store.list_metadata()

    assert {e["id"] for e in listed} == {
        _id_of(_sample_item(chunk_index=0)), _id_of(_sample_item(chunk_index=1)),
    }
    assert {e["metadata"]["embedding_model"] for e in listed} == {"resolved/model-xyz"}
    assert {e["metadata"]["parent_context"] for e in listed} == {"/ingest/root"}


def test_chunker_returns_hash_index_and_estimate_per_chunk() -> None:
    """Tier 2b: #2972 -- each chunk carries its own content_hash /
    chunk_index / est_tokens, so the ingest pipeline needs no python of its
    own to derive them (R1 has no hash / enumerate / string-length).

    Asserts the three behaviourally, not merely present: the hash tracks the
    TEXT (identical text -> identical hash, changed text -> changed hash --
    which is what makes it usable as a change-detection key), the index is
    the 0-based position, and est_tokens is the chars/4 embedding-cost
    estimate rather than an echo of chonkie's own token_count."""
    text = "".join(f"Sentence number {i} continues the document. " for i in range(200))
    chunks = chunk_text(text, size=64, overlap_ratio=0.0)

    # Positions ascend 0,1,2,... -- and reading chunks[1] keeps this
    # non-vacuous (a single-chunk result would raise rather than pass).
    assert chunks[1]["chunk_index"] == 1
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))

    # Same text -> same hash (dedup would skip); different text -> different.
    again = chunk_text(text, size=64, overlap_ratio=0.0)
    assert [c["content_hash"] for c in again] == [c["content_hash"] for c in chunks]
    changed = chunk_text(text.replace("Sentence number 0", "REWRITTEN"), size=64, overlap_ratio=0.0)
    assert changed[0]["content_hash"] != chunks[0]["content_hash"]

    for c in chunks:
        assert c["est_tokens"] == max(1, len(c["text"]) // 4)


def test_chunker_size_and_overlap_are_real_parameters() -> None:
    """Tier 2b: R4 -- size/overlap are tool parameters with real effect, not baked-in
    constants -- smaller size yields more chunks; nonzero overlap yields
    larger (suffix-merged) token counts on non-final chunks."""
    text = "".join(f"Sentence number {i} continues the document. " for i in range(400))

    small_chunks = chunk_text(text, size=32, overlap_ratio=0.0)
    big_chunks = chunk_text(text, size=128, overlap_ratio=0.0)
    assert len(small_chunks) > len(big_chunks)

    no_overlap = chunk_text(text, size=64, overlap_ratio=0.0)
    with_overlap = chunk_text(text, size=64, overlap_ratio=0.25)
    assert with_overlap[1]["token_count"] > no_overlap[1]["token_count"]


def test_chunker_default_lands_in_2026_persistent_rag_band() -> None:
    """Tier 2b: R4/co-vet #3 -- the DEFAULT size (no override) is the 256-512-token
    persistent-RAG band, not FP-0057 line 51's 800-1024 ephemeral-attachment
    figure."""
    text = "".join(f"Sentence number {i} continues the document. " for i in range(400))
    chunks = chunk_text(text)  # defaults only
    # Every non-final chunk should be close to the 256-512 band (chonkie's
    # recursive splitter respects sentence boundaries so exact equality to
    # `size` isn't guaranteed, but it must not silently reproduce the
    # 800-1024 ephemeral figure).
    assert all(c["token_count"] <= 512 for c in chunks[:-1])
