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
    assert _C._suffix_no_dot(path) == expected
