"""Tier 2 — chunkers_safe.write_chunks_with_lock contract tests (FP-0042 Phase 2.2).

Tests the safe-mode postprocessor step that replaced chunkers.py's
unsafe-mode ``write_chunks_with_lock`` (= advisory lock + chunked
JSONL write). Coverage mirrors the lock-discipline tests previously
exercised against the deprecated ``apply_strategy`` so the contract
stays observably stable across the migration.

The function reads source files + writes ``artifacts/chunks.jsonl`` +
acquires ``.reyn/index/<source>/.lock`` — all through reyn.safe.file.
The autouse fixture sets a permission context that grants reads
under tmp_path and writes under tmp_path/.reyn/ + tmp_path/artifacts/.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from reyn.safe import file as sf


def _load_chunkers_safe():
    """Import chunkers_safe.py from the skill directory."""
    skill_dir = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "index_docs"
    )
    spec = importlib.util.spec_from_file_location(
        "chunkers_safe", skill_dir / "chunkers_safe.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_C = _load_chunkers_safe()


@pytest.fixture(autouse=True)
def _reset_safe_file_context():
    """Reset reyn.safe.file's module-global permission context per test."""
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False
    yield
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Wire a sandbox tmpdir: cwd here, safe.file context grants reads
    under tmp_path and writes under tmp_path/.reyn + tmp_path/artifacts.

    Mirrors how the production preprocessor_executor wires the
    subprocess's permission context (= CWD as default read zone;
    .reyn/ as default write zone; plus explicit artifacts/ from
    skill.md).
    """
    monkeypatch.chdir(tmp_path)
    sf._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[
            str(tmp_path / ".reyn"),
            str(tmp_path / "artifacts"),
        ],
    )
    return tmp_path


def _make_artifact(data: dict) -> dict:
    return {"type": "chunk_strategy", "data": data}


def _strategy_payload(*, source: str, file_paths: list[str]) -> dict:
    return {
        "boundary": "blank_line",
        "max_chunk_size_tokens": 500,
        "min_chunk_size_tokens": 1,
        "overlap_ratio": 0.0,
        "preserve_parent_context": True,
        "source": source,
        "chunk_list": [{"source_path": p} for p in file_paths],
        "mode": "append",
        "description": "test docs",
        "path": "irrelevant — chunk_list drives the work",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_write_chunks_writes_jsonl_under_artifacts(sandbox: Path):
    """Tier 2: write_chunks_with_lock writes artifacts/chunks.jsonl with one
    JSON object per line, summary dict reflects the count."""
    md = sandbox / "doc.md"
    md.write_text("Hello world.\n\nSecond paragraph.\n", encoding="utf-8")

    artifact = _make_artifact(
        _strategy_payload(source="s1", file_paths=[str(md)])
    )
    result = _C.write_chunks_with_lock(artifact)

    assert result["chunk_count"] > 0
    assert result["source_lock_acquired"] is True
    assert result["chunks_path"] == "artifacts/chunks.jsonl"

    chunks_path = sandbox / "artifacts" / "chunks.jsonl"
    assert chunks_path.exists()
    lines = [json.loads(l) for l in chunks_path.read_text().splitlines() if l.strip()]
    assert len(lines) == result["chunk_count"]
    for ln in lines:
        assert "text" in ln
        assert "metadata" in ln
        assert "content_hash" in ln["metadata"]
        assert "source_path" in ln["metadata"]


def test_write_chunks_lock_released_on_success(sandbox: Path):
    """Tier 2: the .lock file is deleted after a successful run."""
    md = sandbox / "doc.md"
    md.write_text("body", encoding="utf-8")

    artifact = _make_artifact(
        _strategy_payload(source="release_test", file_paths=[str(md)])
    )
    _C.write_chunks_with_lock(artifact)

    lock_path = sandbox / ".reyn" / "index" / "release_test" / ".lock"
    assert not lock_path.exists()


def test_write_chunks_lock_released_on_error(sandbox: Path):
    """Tier 2: the .lock file is deleted even if the chunking raises.

    Simulate an inner failure by passing a non-list ``chunk_list`` (=
    the iteration over ``data.chunk_list`` raises). The lock acquire
    happens before that, so the ``finally`` block must reap it.
    """
    artifact = _make_artifact(_strategy_payload(source="err_test", file_paths=[]))
    artifact["data"]["chunk_list"] = 42  # not iterable as a list of dicts

    with pytest.raises(Exception):
        _C.write_chunks_with_lock(artifact)

    lock_path = sandbox / ".reyn" / "index" / "err_test" / ".lock"
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# Lock semantics — concurrent + stale
# ---------------------------------------------------------------------------


def test_write_chunks_blocks_when_holder_alive(sandbox: Path):
    """Tier 2: a pre-existing lock with the current PID (= alive) blocks
    the new run with a RuntimeError mentioning the holder."""
    source = "concurrent_test"
    lock_dir = sandbox / ".reyn" / "index" / source
    lock_dir.mkdir(parents=True)
    lock_path = lock_dir / ".lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "ts": 0.0}), encoding="utf-8"
    )

    md = sandbox / "doc.md"
    md.write_text("body", encoding="utf-8")
    artifact = _make_artifact(
        _strategy_payload(source=source, file_paths=[str(md)])
    )

    with pytest.raises(RuntimeError, match="currently being indexed"):
        _C.write_chunks_with_lock(artifact)


def test_write_chunks_reaps_stale_lock(sandbox: Path):
    """Tier 2: a lock with a dead PID is reaped; the run completes."""
    source = "stale_test"
    lock_dir = sandbox / ".reyn" / "index" / source
    lock_dir.mkdir(parents=True)
    lock_path = lock_dir / ".lock"
    # PID 2_000_000_000 is past any plausible kernel.pid_max.
    lock_path.write_text(
        json.dumps({"pid": 2_000_000_000, "ts": 0.0}), encoding="utf-8"
    )

    md = sandbox / "doc.md"
    md.write_text("body", encoding="utf-8")
    artifact = _make_artifact(
        _strategy_payload(source=source, file_paths=[str(md)])
    )

    result = _C.write_chunks_with_lock(artifact)
    assert result["chunk_count"] > 0
    assert result["source_lock_acquired"] is True


def test_write_chunks_takes_over_corrupted_lock(sandbox: Path):
    """Tier 2: a corrupted lock (= invalid JSON) is taken over silently."""
    source = "corrupt_test"
    lock_dir = sandbox / ".reyn" / "index" / source
    lock_dir.mkdir(parents=True)
    lock_path = lock_dir / ".lock"
    lock_path.write_text("not valid json {{", encoding="utf-8")

    md = sandbox / "doc.md"
    md.write_text("body", encoding="utf-8")
    artifact = _make_artifact(
        _strategy_payload(source=source, file_paths=[str(md)])
    )

    result = _C.write_chunks_with_lock(artifact)
    assert result["chunk_count"] > 0


# ---------------------------------------------------------------------------
# Permission denial surface
# ---------------------------------------------------------------------------


def test_write_chunks_skips_unreadable_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tier 2: a source path outside the declared read zone is silently
    skipped (matches the legacy OSError-swallowing read loop). When all
    inputs are denied, the JSONL is empty and chunk_count is 0.
    """
    monkeypatch.chdir(tmp_path)
    grant = tmp_path / "ok"
    grant.mkdir()
    denied = tmp_path / "denied"
    denied.mkdir()
    md = denied / "secret.md"
    md.write_text("secret content", encoding="utf-8")

    sf._set_permission_context(
        # Grant reads where there are no source files, plus the lock dir
        # so write_chunks_with_lock can probe / read its own lock. Source
        # files in tmp_path/denied/ stay unreadable, which is the path under
        # test.
        read_paths=[str(grant), str(tmp_path / ".reyn")],
        write_paths=[
            str(tmp_path / ".reyn"),
            str(tmp_path / "artifacts"),
        ],
    )

    artifact = _make_artifact(
        _strategy_payload(source="denied_test", file_paths=[str(md)])
    )
    result = _C.write_chunks_with_lock(artifact)

    assert result["chunk_count"] == 0
    chunks_path = tmp_path / "artifacts" / "chunks.jsonl"
    # Output file is written (possibly empty) — the writer always produces it.
    assert chunks_path.exists()


# ---------------------------------------------------------------------------
# Helper pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("foo.md", "md"),
        ("/a/b/c.py", "py"),
        ("noext", ""),
        (".hidden", ""),
        (".hidden.md", "md"),
        ("nested.dir/file.txt", "txt"),
    ],
)
def test_suffix_no_dot(path: str, expected: str):
    """Tier 2: _suffix_no_dot strips the leading dot; mirrors
    pathlib.PurePath.suffix.lstrip(".") for the production paths."""
    assert _C.suffix_no_dot(path) == expected


# ---------------------------------------------------------------------------
# Split-strategy invariants (moved from test_chunkers.py when the deprecated
# chunkers.py + apply_strategy were retired post-FP-0042 Phase 2.8).
# Targets the identical split helpers duplicated in chunkers_safe.py for the
# write_chunks_with_lock path.
# ---------------------------------------------------------------------------


def test_split_heading_simple_markdown():
    """Tier 2b: _split_heading on structured Markdown returns per-heading chunks."""
    text = "# Title\n\nIntro text.\n\n## Section A\n\nContent A.\n\n## Section B\n\nContent B."
    chunks = list(_C._split_heading(text, max_size=500, min_size=1, overlap=0.0))

    assert chunks, "expected at least one chunk from headed Markdown"
    assert all(ctx is not None for _, ctx in chunks)
    texts = [t for t, _ in chunks]
    assert any("Title" in t for t in texts)
    assert any("Section A" in t or "Section B" in t for t in texts)


def test_split_heading_fallback_no_headings():
    """Tier 2: _split_heading falls back to blank_line when no headings present."""
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    chunks = list(_C._split_heading(text, max_size=500, min_size=1, overlap=0.0))
    assert len(chunks) >= 1


def test_split_heading_large_section_sub_splits():
    """Tier 2b: _split_heading sub-splits a section that exceeds max_size."""
    many_paras = "\n\n".join(["word " * 20] * 10)
    text = f"# Big Section\n\n{many_paras}"
    chunks = list(_C._split_heading(text, max_size=10, min_size=1, overlap=0.0))
    # A section far exceeding max_size must produce multiple chunks.
    assert chunks, "expected chunks from oversized section"
    total_text = " ".join(t for t, _ in chunks)
    assert "word" in total_text, "chunked content must appear in output"
    assert all(
        _C.approx_tokens(t) <= 30 for t, _ in chunks
    ), "each chunk must be near or under max_size"


def test_split_blank_line_packs_paragraphs_into_max_size():
    """Tier 2b: _split_blank_line packs paragraphs into chunks <= max_size."""
    paras = [f"Paragraph {i} with some text here." for i in range(10)]
    text = "\n\n".join(paras)
    chunks = list(_C._split_blank_line(text, max_size=30, min_size=1, overlap=0.0))

    assert chunks, "expected chunks from 10-paragraph text"
    for chunk_text, _ in chunks:
        assert _C.approx_tokens(chunk_text) <= 40


def test_split_blank_line_parent_context_is_none():
    """Tier 2: _split_blank_line always yields None as parent_context."""
    text = "Para one.\n\nPara two.\n\nPara three."
    for _, parent_ctx in _C._split_blank_line(text, max_size=200, min_size=1, overlap=0.0):
        assert parent_ctx is None


def test_split_blank_line_respects_min_size():
    """Tier 2: _split_blank_line discards chunks smaller than min_size."""
    text = "Tiny.\n\nA longer paragraph with sufficient content to pass min size check."
    chunks = list(_C._split_blank_line(text, max_size=1000, min_size=20, overlap=0.0))
    for chunk_text, _ in chunks:
        assert _C.approx_tokens(chunk_text) >= 5


def test_split_sentence_splits_at_sentence_boundaries():
    """Tier 2b: _split_sentence splits at sentence end (., !, ?)."""
    text = "First sentence. Second sentence. Third sentence! Fourth sentence? Fifth."
    chunks = list(_C._split_sentence(text, max_size=10, min_size=1, overlap=0.0))

    assert chunks, "expected multiple chunks from multi-sentence text at low max_size"
    joined = " ".join(t for t, _ in chunks)
    assert "First" in joined
    # Content must be distributed — not one single blob
    assert any("Second" in t or "Third" in t for t, _ in chunks)


def test_split_sentence_no_sentence_boundary_single_chunk():
    """Tier 2b: _split_sentence yields single chunk when text has no sentence boundaries."""
    text = "no terminal punctuation in this long run-on line with many words"
    chunks = list(_C._split_sentence(text, max_size=1000, min_size=1, overlap=0.0))

    assert chunks, "expected at least one chunk from non-empty text"
    # All content must appear in the (single) output chunk — nothing dropped.
    joined = " ".join(t for t, _ in chunks)
    assert "punctuation" in joined
    assert chunks[0][1] is None


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
