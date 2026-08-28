"""Tier 2: #5427 — SqliteIndexBackend.query's own float32 cosine
computation can round a near-parallel pair's score 1 ulp past the
mathematical [-1.0, 1.0] range; the real production consumer's own
contract (ActionEmbeddingIndex.query's docstring) promises that range,
so this clamps in the implementation rather than leaving every caller
(and every test asserting the documented range) to tolerate float32's
looser one.

Real overshoot, not a synthetic value: a real, deterministic float32
vector pair (seeded ``numpy.random.randn(8)``, self-matched) reproduces
the EXACT score CI observed (``1.0000001192092896``) — verified by hand
this session before writing this test.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from reyn.data.index.backend import ChunkRecord
from reyn.data.index.backends.sqlite import SqliteIndexBackend


def _overshooting_vector() -> "list[float]":
    """A real float32 vector whose self-cosine (dot(v, v) / (|v| * |v|))
    rounds to 1.0000001192092896 — the exact value CI observed
    (#5427's own issue, #5424's CI run). Deterministic (fixed seed),
    hand-verified this session: `np.random.seed(0)` then
    `np.random.randn(8)` 10 times, the 10th draw (index 9) is this
    overshooting pair."""
    rng = np.random.default_rng()
    rng = np.random.RandomState(0)  # noqa: NPY002 — matches the exact repro seed
    v = None
    for _ in range(10):
        v = rng.randn(8).astype(np.float32)
    assert v is not None
    return [float(x) for x in v]


def test_the_repro_vector_genuinely_overshoots_in_raw_numpy():
    """Tier 1: non-vacuity — confirms the fixture vector actually
    produces a score > 1.0 in RAW float32 numpy arithmetic (the same
    computation the backend performs), before checking the backend
    clamps it. Without this, a fixture that never actually overshoots
    would make the backend test below pass for the wrong reason."""
    v = np.asarray(_overshooting_vector(), dtype=np.float32)
    raw_score = float((v @ v) / (float(np.linalg.norm(v)) ** 2))
    assert raw_score > 1.0, (
        f"fixture vector must genuinely overshoot in raw float32 "
        f"arithmetic to be a real positive control; got {raw_score!r}"
    )


@pytest.mark.asyncio
async def test_self_match_score_is_clamped_to_one(tmp_path: Path) -> None:
    """Tier 2: #5427's own witness — a real self-match through the
    REAL backend (write then query with the identical vector) must
    return a score that is never > 1.0, even though the raw float32
    arithmetic (confirmed by the sibling test above) would produce
    1.0000001192092896 for this exact vector.

    Strip-falsifier: removing the ``np.clip(scores, -1.0, 1.0)`` line
    in sqlite.py's query() turns this red — the real score returned is
    1.0000001192092896, greater than 1.0. Verified by hand this
    session."""
    vector = _overshooting_vector()
    backend = SqliteIndexBackend(workspace_root=tmp_path)
    chunk = ChunkRecord(
        text="self-match probe",
        vector=vector,
        metadata={
            "source_path": "file.txt",
            "source_type": "generic",
            "content_hash": "h-5427",
            "embedding_model": "test-model",
            "chunk_index": 0,
            "size_tokens": 1,
            "parent_context": None,
        },
        score=None,
    )
    await backend.write("src-5427", [chunk], mode="append")

    hits = await backend.query("src-5427", vector, top_k=1, filters={})
    (top,) = hits
    assert top["score"] is not None
    assert top["score"] <= 1.0, (
        f"#5427 REGRESSION: a self-match's cosine score exceeded the "
        f"documented [-1.0, 1.0] range — got {top['score']!r}"
    )
    assert top["score"] >= -1.0
