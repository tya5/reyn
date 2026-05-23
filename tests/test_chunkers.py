"""Tier 2: OS invariant tests for index_docs stdlib skill chunkers.

Tests the deterministic chunking logic (apply_strategy, _split_*,
write_chunks_with_lock helpers) used by the index_docs skill
(ADR-0033 §2.1).

Coverage for the safe-mode preprocessor steps (``gather_samples`` /
``cost_preflight``) lives in ``test_chunkers_preproc_safe.py`` — they
moved out of this module to ``chunkers_preproc_safe.py`` as part of
FP-0042 Phase 2.1.

No mocks; uses real filesystem operations via tmp_path.
Tests cover UX gap fix D (concurrent lock detection in apply_strategy).
"""
from __future__ import annotations

import hashlib

# ---------------------------------------------------------------------------
# Import target functions
# ---------------------------------------------------------------------------
# Resolve the chunkers module path directly — it's a skill-local .py file,
# not a regular Python module. Import via importlib to mirror the harness.
import importlib.util
import json
import os
from pathlib import Path

import pytest


def _load_chunkers():
    """Import chunkers.py from the skill directory."""
    skill_dir = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_docs"
    )
    spec = importlib.util.spec_from_file_location(
        "chunkers", skill_dir / "chunkers.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_C = _load_chunkers()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk_strategy_artifact(data: dict) -> dict:
    """Wrap data in chunk_strategy artifact envelope."""
    return {"type": "chunk_strategy", "data": data}


# ---------------------------------------------------------------------------
# _split_heading
# ---------------------------------------------------------------------------


def test_split_heading_simple_markdown():
    """Tier 2: _split_heading on structured Markdown returns per-heading chunks."""
    text = "# Title\n\nIntro text.\n\n## Section A\n\nContent A.\n\n## Section B\n\nContent B."
    chunks = list(_C._split_heading(text, max_size=500, min_size=1, overlap=0.0))

    assert len(chunks) >= 2
    # Each chunk should carry a heading label as parent_context
    assert all(ctx is not None for _, ctx in chunks)
    # First heading label should contain "Title"
    texts = [t for t, _ in chunks]
    assert any("Title" in t for t in texts)


def test_split_heading_fallback_no_headings():
    """Tier 2: _split_heading falls back to blank_line when no headings present."""
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    chunks = list(_C._split_heading(text, max_size=500, min_size=1, overlap=0.0))

    # Should still yield something (blank_line fallback)
    assert len(chunks) >= 1


def test_split_heading_large_section_sub_splits():
    """Tier 2: _split_heading sub-splits a section that exceeds max_size."""
    # Build a section with many paragraphs that exceed 10-token max_size
    many_paras = "\n\n".join(["word " * 20] * 10)  # ~200 tokens per group
    text = f"# Big Section\n\n{many_paras}"
    chunks = list(_C._split_heading(text, max_size=10, min_size=1, overlap=0.0))

    # Should have multiple chunks since section is large
    assert len(chunks) > 1


# ---------------------------------------------------------------------------
# _split_blank_line
# ---------------------------------------------------------------------------


def test_split_blank_line_packs_paragraphs_into_max_size():
    """Tier 2: _split_blank_line packs paragraphs into chunks <= max_size."""
    # Each paragraph is ~10 tokens (40 chars)
    paras = [f"Paragraph {i} with some text here." for i in range(10)]
    text = "\n\n".join(paras)

    # max_size = 30 tokens — each paragraph ~10 tokens; so ~3 per chunk
    chunks = list(_C._split_blank_line(text, max_size=30, min_size=1, overlap=0.0))

    assert len(chunks) >= 3
    for chunk_text, _ in chunks:
        assert _C._approx_tokens(chunk_text) <= 40  # slight overshoot allowed


def test_split_blank_line_parent_context_is_none():
    """Tier 2: _split_blank_line always yields None as parent_context."""
    text = "Para one.\n\nPara two.\n\nPara three."
    for _, parent_ctx in _C._split_blank_line(text, max_size=200, min_size=1, overlap=0.0):
        assert parent_ctx is None


def test_split_blank_line_respects_min_size():
    """Tier 2: _split_blank_line discards chunks smaller than min_size."""
    text = "Tiny.\n\nA longer paragraph with sufficient content to pass min size check."
    chunks = list(_C._split_blank_line(text, max_size=1000, min_size=20, overlap=0.0))

    # "Tiny." is ~1 token — should be merged or discarded
    for chunk_text, _ in chunks:
        assert _C._approx_tokens(chunk_text) >= 5


# ---------------------------------------------------------------------------
# _split_sentence
# ---------------------------------------------------------------------------


def test_split_sentence_splits_at_sentence_boundaries():
    """Tier 2: _split_sentence splits at sentence end (., !, ?)."""
    text = "First sentence. Second sentence. Third sentence! Fourth sentence? Fifth."
    chunks = list(_C._split_sentence(text, max_size=10, min_size=1, overlap=0.0))

    # With max_size=10 tokens and ~3 tokens per sentence, expect multiple chunks
    assert len(chunks) >= 2
    # All chunks should end with a sentence fragment
    joined = " ".join(t for t, _ in chunks)
    assert "First" in joined


def test_split_sentence_no_sentence_boundary_single_chunk():
    """Tier 2: _split_sentence yields single chunk when text has no sentence boundaries."""
    text = "no terminal punctuation in this long run-on line with many words"
    chunks = list(_C._split_sentence(text, max_size=1000, min_size=1, overlap=0.0))

    assert len(chunks) == 1
    assert chunks[0][1] is None  # no parent_context


# ---------------------------------------------------------------------------
# _split fallback for unknown boundary
# ---------------------------------------------------------------------------


def test_split_fallback_unknown_boundary():
    """Tier 2: _split falls back to blank_line for unknown boundary type."""
    text = "Para one.\n\nPara two.\n\nPara three."
    chunks_unknown = list(
        _C._split(text, "unknown_boundary", max_size=200, min_size=1, overlap=0.0)
    )
    chunks_blank = list(
        _C._split(text, "blank_line", max_size=200, min_size=1, overlap=0.0)
    )

    assert len(chunks_unknown) == len(chunks_blank)
    assert [t for t, _ in chunks_unknown] == [t for t, _ in chunks_blank]


# ---------------------------------------------------------------------------
# apply_strategy
# ---------------------------------------------------------------------------


def test_apply_strategy_heading_boundary_with_parent_context(tmp_path):
    """Tier 2: apply_strategy with heading boundary yields chunks with parent_context."""
    md_content = "# Section One\n\nContent of section one.\n\n## Subsection\n\nSub content."
    (tmp_path / "doc.md").write_text(md_content, encoding="utf-8")

    artifact = _make_chunk_strategy_artifact(
        {
            "boundary": "heading",
            "max_chunk_size_tokens": 500,
            "min_chunk_size_tokens": 1,
            "overlap_ratio": 0.0,
            "preserve_parent_context": True,
            "source": "test_src",
            "path": str(tmp_path / "*.md"),
            "description": "Test docs",
            "mode": "append",
        }
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = _C.apply_strategy(artifact)
    finally:
        os.chdir(old_cwd)

    assert result["chunk_count"] > 0
    assert result["source_lock_acquired"] is True
    assert result["chunks_path"] == "artifacts/chunks.jsonl"

    # Verify JSONL was written
    chunks_path = tmp_path / "artifacts" / "chunks.jsonl"
    assert chunks_path.exists()
    lines = [json.loads(l) for l in chunks_path.read_text().splitlines() if l.strip()]
    assert len(lines) == result["chunk_count"]

    # At least one chunk should have a non-null parent_context (heading)
    parent_contexts = [ln["metadata"]["parent_context"] for ln in lines]
    assert any(ctx is not None for ctx in parent_contexts)


def test_apply_strategy_blank_line_on_python_file(tmp_path):
    """Tier 2: apply_strategy with blank_line on Python file returns chunks, parent_context=None."""
    py_content = "def foo():\n    pass\n\n\ndef bar():\n    return 42\n\n\nx = 1\n"
    (tmp_path / "script.py").write_text(py_content, encoding="utf-8")

    artifact = _make_chunk_strategy_artifact(
        {
            "boundary": "blank_line",
            "max_chunk_size_tokens": 500,
            "min_chunk_size_tokens": 1,
            "overlap_ratio": 0.0,
            "preserve_parent_context": False,
            "source": "pycode",
            "path": str(tmp_path / "*.py"),
            "description": "Python scripts",
            "mode": "append",
        }
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = _C.apply_strategy(artifact)
    finally:
        os.chdir(old_cwd)

    assert result["chunk_count"] > 0
    chunks_path = tmp_path / "artifacts" / "chunks.jsonl"
    lines = [json.loads(l) for l in chunks_path.read_text().splitlines() if l.strip()]
    # preserve_parent_context=False → parent_context is None in all chunks
    for ln in lines:
        assert ln["metadata"]["parent_context"] is None


def test_apply_strategy_assigns_sequential_chunk_index(tmp_path):
    """Tier 2: apply_strategy assigns sequential chunk_index starting from 0."""
    (tmp_path / "a.md").write_text("Para one.\n\nPara two.", encoding="utf-8")

    artifact = _make_chunk_strategy_artifact(
        {
            "boundary": "blank_line",
            "max_chunk_size_tokens": 10,
            "min_chunk_size_tokens": 1,
            "overlap_ratio": 0.0,
            "preserve_parent_context": True,
            "source": "seq_test",
            "path": str(tmp_path / "*.md"),
            "description": "Test",
            "mode": "append",
        }
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = _C.apply_strategy(artifact)
    finally:
        os.chdir(old_cwd)

    chunks_path = tmp_path / "artifacts" / "chunks.jsonl"
    lines = [json.loads(l) for l in chunks_path.read_text().splitlines() if l.strip()]
    indices = [ln["metadata"]["chunk_index"] for ln in lines]
    assert indices == list(range(len(lines)))


def test_apply_strategy_content_hash_is_deterministic(tmp_path):
    """Tier 2: apply_strategy assigns sha256 content_hash; same content = same hash."""
    text = "Deterministic content for hash verification."
    (tmp_path / "doc.md").write_text(text, encoding="utf-8")

    artifact = _make_chunk_strategy_artifact(
        {
            "boundary": "blank_line",
            "max_chunk_size_tokens": 500,
            "min_chunk_size_tokens": 1,
            "overlap_ratio": 0.0,
            "preserve_parent_context": True,
            "source": "hash_test",
            "path": str(tmp_path / "*.md"),
            "description": "Test",
            "mode": "append",
        }
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _C.apply_strategy(artifact)
    finally:
        os.chdir(old_cwd)

    chunks_path = tmp_path / "artifacts" / "chunks.jsonl"
    lines = [json.loads(l) for l in chunks_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1

    chunk_text = lines[0]["text"]
    expected_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
    assert lines[0]["metadata"]["content_hash"] == expected_hash


def test_apply_strategy_lock_released_after_success(tmp_path):
    """Tier 2: apply_strategy releases the .lock file after successful completion."""
    (tmp_path / "doc.md").write_text("Content.", encoding="utf-8")

    artifact = _make_chunk_strategy_artifact(
        {
            "boundary": "blank_line",
            "max_chunk_size_tokens": 500,
            "min_chunk_size_tokens": 1,
            "overlap_ratio": 0.0,
            "preserve_parent_context": True,
            "source": "lock_release_test",
            "path": str(tmp_path / "*.md"),
            "description": "Test",
            "mode": "append",
        }
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _C.apply_strategy(artifact)
    finally:
        os.chdir(old_cwd)

    lock_path = tmp_path / ".reyn" / "index" / "lock_release_test" / ".lock"
    assert not lock_path.exists(), "Lock file should be removed after successful run"


def test_apply_strategy_concurrent_lock_raises(tmp_path):
    """Tier 2: apply_strategy raises RuntimeError when source is locked by live PID."""
    source = "concurrent_test"
    lock_path = tmp_path / ".reyn" / "index" / source / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a lock with our own PID (= alive)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "ts": 0.0}), encoding="utf-8"
    )

    (tmp_path / "doc.md").write_text("Content.", encoding="utf-8")

    artifact = _make_chunk_strategy_artifact(
        {
            "boundary": "blank_line",
            "max_chunk_size_tokens": 500,
            "min_chunk_size_tokens": 1,
            "overlap_ratio": 0.0,
            "preserve_parent_context": True,
            "source": source,
            "path": str(tmp_path / "*.md"),
            "description": "Test",
            "mode": "append",
        }
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="currently being indexed"):
            _C.apply_strategy(artifact)
    finally:
        os.chdir(old_cwd)

    # Clean up lock
    lock_path.unlink(missing_ok=True)


def test_apply_strategy_stale_lock_is_reaped(tmp_path):
    """Tier 2: apply_strategy replaces stale locks (dead PID) and proceeds normally."""
    source = "stale_lock_test"
    lock_path = tmp_path / ".reyn" / "index" / source / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a lock with a dead PID (PID 1 is always init/launchd, not a user process)
    # Use PID 999999 which is almost certainly not running
    lock_path.write_text(
        json.dumps({"pid": 999999, "ts": 0.0}), encoding="utf-8"
    )

    (tmp_path / "doc.md").write_text("Stale lock content.", encoding="utf-8")

    artifact = _make_chunk_strategy_artifact(
        {
            "boundary": "blank_line",
            "max_chunk_size_tokens": 500,
            "min_chunk_size_tokens": 1,
            "overlap_ratio": 0.0,
            "preserve_parent_context": True,
            "source": source,
            "path": str(tmp_path / "*.md"),
            "description": "Test",
            "mode": "append",
        }
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = _C.apply_strategy(artifact)
    finally:
        os.chdir(old_cwd)

    assert result["chunk_count"] > 0
    assert result["source_lock_acquired"] is True
