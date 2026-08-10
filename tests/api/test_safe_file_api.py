"""Tier 2 — reyn.api.safe.file permission-gated file API contract tests (FP-0042).

The :mod:`reyn.api.safe.file` module exposes read / write / glob / exists / stat
/ open to safe-mode python steps with a path-list permission check. These
tests pin the gate behaviour: declared-path-in → grant, declared-path-out
→ deny with :class:`PermissionError`, context-not-set → deny, ``open`` mode
selects read-vs-write gate correctly, and the returned file object is a
real ``io.TextIOBase`` so stdlib libraries work against it.

The permission-context globals on the module are reset before every test
via the :func:`_reset_context` fixture so test order does not leak state.
"""
from __future__ import annotations

import io
import json as _stdlib_json
from pathlib import Path

import pytest

from reyn.api.safe import file as sf


@pytest.fixture(autouse=True)
def _reset_context():
    """Reset reyn.api.safe.file's module-global permission context.

    Tests that need a permission context call ``sf._set_permission_context``
    explicitly. The reset ensures order-independent runs and matches the
    "fresh subprocess per step" production behaviour.
    """
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False
    yield
    sf._read_paths = ()
    sf._write_paths = ()
    sf._context_initialised = False


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Create a sandbox tmpdir with one readable + one writable subdir."""
    read_dir = tmp_path / "docs"
    read_dir.mkdir()
    (read_dir / "a.md").write_text("hello A\n", encoding="utf-8")
    (read_dir / "b.md").write_text("hello B\n", encoding="utf-8")
    write_dir = tmp_path / "out"
    write_dir.mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Context-not-initialised path
# ---------------------------------------------------------------------------


def test_read_without_context_raises() -> None:
    """Tier 2: bare-process read fails when no permission context is set.

    The harness initialises the context before user code runs; if a
    caller invokes the API outside that flow, the helpful error
    message must point at ``_set_permission_context``.
    """
    with pytest.raises(PermissionError) as exc:
        sf.read("README.md")
    assert "permission context not initialised" in str(exc.value)
    assert "_set_permission_context" in str(exc.value)


def test_write_without_context_raises() -> None:
    """Tier 2: same as read but for write."""
    with pytest.raises(PermissionError):
        sf.write("/tmp/x", "content")


# ---------------------------------------------------------------------------
# Path inclusion / exclusion
# ---------------------------------------------------------------------------


def test_read_within_read_paths_returns_content(sandbox: Path) -> None:
    """Tier 2: declared read path grants access; content matches."""
    sf._set_permission_context(read_paths=[str(sandbox / "docs")])
    content = sf.read(str(sandbox / "docs" / "a.md"))
    assert content == "hello A\n"


def test_read_outside_read_paths_raises(sandbox: Path) -> None:
    """Tier 2: read of a sibling-dir file is denied with an actionable error."""
    sf._set_permission_context(read_paths=[str(sandbox / "docs")])
    target = sandbox / "out" / "stray.md"
    target.write_text("private\n", encoding="utf-8")
    with pytest.raises(PermissionError) as exc:
        sf.read(str(target))
    msg = str(exc.value)
    assert "not in the declared read_paths" in msg
    assert "file.read:" in msg  # mentions the skill.md fix


def test_write_within_write_paths_writes(sandbox: Path) -> None:
    """Tier 2: declared write path grants write; file content lands."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "result.txt"
    sf.write(str(target), "payload\n")
    assert target.read_text() == "payload\n"


def test_write_outside_write_paths_raises(sandbox: Path) -> None:
    """Tier 2: write outside declared paths denies even when target exists."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    with pytest.raises(PermissionError):
        sf.write(str(sandbox / "docs" / "a.md"), "overwrite attempt")
    # Original content untouched.
    assert (sandbox / "docs" / "a.md").read_text() == "hello A\n"


# ---------------------------------------------------------------------------
# Boundary cases — prefix matching
# ---------------------------------------------------------------------------


def test_path_prefix_does_not_match_partial_segment(sandbox: Path) -> None:
    """Tier 2: ``/foo/bar`` access does NOT grant ``/foo/barbaz``.

    The allowed prefix is a directory-boundary prefix, not a string
    prefix. Without the ``os.sep`` guard, ``/foo/bar`` would falsely
    match ``/foo/barbaz/x.md``. This catches accidental authority
    leak across sibling directory names.
    """
    bar = sandbox / "bar"
    barbaz = sandbox / "barbaz"
    bar.mkdir()
    barbaz.mkdir()
    (barbaz / "x.md").write_text("not yours\n", encoding="utf-8")
    sf._set_permission_context(read_paths=[str(bar)])
    with pytest.raises(PermissionError):
        sf.read(str(barbaz / "x.md"))


def test_exact_path_match_grants_access(sandbox: Path) -> None:
    """Tier 2: a single-file ``read_paths`` entry grants exactly that file."""
    target = sandbox / "docs" / "a.md"
    sf._set_permission_context(read_paths=[str(target)])
    assert sf.read(str(target)) == "hello A\n"
    # But not its sibling.
    with pytest.raises(PermissionError):
        sf.read(str(sandbox / "docs" / "b.md"))


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def test_exists_gated_as_read(sandbox: Path) -> None:
    """Tier 2: ``exists`` is permission-checked as a read.

    Existence probing is an observation that requires the same
    authority as reading the file (= protects against directory
    enumeration outside declared paths).
    """
    sf._set_permission_context(read_paths=[str(sandbox / "docs")])
    assert sf.exists(str(sandbox / "docs" / "a.md")) is True
    assert sf.exists(str(sandbox / "docs" / "missing.md")) is False
    with pytest.raises(PermissionError):
        sf.exists(str(sandbox / "out" / "anything"))


def test_stat_gated_as_read(sandbox: Path) -> None:
    """Tier 2: ``stat`` is permission-checked as a read; returns the
    documented shape ``{size, mtime, mode}``."""
    sf._set_permission_context(read_paths=[str(sandbox / "docs")])
    info = sf.stat(str(sandbox / "docs" / "a.md"))
    assert set(info.keys()) == {"size", "mtime", "mode"}
    assert info["size"] == len("hello A\n")
    with pytest.raises(PermissionError):
        sf.stat(str(sandbox / "out"))


def test_glob_does_not_require_context(sandbox: Path) -> None:
    """Tier 2: ``glob`` is metadata-only enumeration, no permission gate.

    The 2026-05-15 R-PURE-MODE stdlib audit endorsed bare ``glob.glob``
    as a safe ambient source — path enumeration without content read.
    Subsequent reads of any returned path still go through :func:`read`
    and are gated there.
    """
    # No _set_permission_context call.
    results = sf.glob(str(sandbox / "docs" / "*.md"))
    assert any(p.endswith("a.md") for p in results)
    assert any(p.endswith("b.md") for p in results)
    assert not any(p.endswith("c.md") for p in results)


# ---------------------------------------------------------------------------
# Low-level open() — IO compatibility
# ---------------------------------------------------------------------------


def test_open_read_returns_real_textiobase(sandbox: Path) -> None:
    """Tier 2: ``sf.open(path, "r")`` returns a real ``io.TextIOBase``.

    The contract is that stdlib libraries (``json.load``, ``csv.reader``,
    ``for line in f``) accept the returned object without adapter. Without
    this, callers couldn't use the low-level API to integrate with stdlib.
    """
    sf._set_permission_context(read_paths=[str(sandbox / "docs")])
    with sf.open(str(sandbox / "docs" / "a.md"), "r") as f:
        assert isinstance(f, io.TextIOBase)
        first = f.read()
    assert first == "hello A\n"


def test_open_supports_iteration_and_seek(sandbox: Path) -> None:
    """Tier 2: returned file object supports ``for line in f`` + ``seek``.

    These are the streaming primitives stdlib libraries expect. The
    permission contract is "may read this path", so byte-level
    re-checks on seek/iter are deliberately absent.
    """
    sf._set_permission_context(read_paths=[str(sandbox / "docs")])
    target = sandbox / "docs" / "a.md"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")
    with sf.open(str(target), "r") as f:
        lines = list(f)
        assert lines == ["line1\n", "line2\n", "line3\n"]
        f.seek(0)
        assert f.read(5) == "line1"


def test_open_compatible_with_stdlib_json_load(sandbox: Path, tmp_path: Path) -> None:
    """Tier 2: ``json.load(sf.open(...))`` works — the canonical demo
    of why the low-level API exists in addition to high-level ``read``.
    """
    target = tmp_path / "data.json"
    target.write_text('{"x": 1, "y": 2}\n', encoding="utf-8")
    sf._set_permission_context(read_paths=[str(tmp_path)])
    with sf.open(str(target), "r") as f:
        loaded = _stdlib_json.load(f)
    assert loaded == {"x": 1, "y": 2}


def test_open_write_mode_uses_write_gate(sandbox: Path) -> None:
    """Tier 2: ``sf.open(path, "w")`` checks against ``write_paths``,
    not ``read_paths``. Cross-axis denial: read-granted-but-write-denied
    path should fail at open time."""
    sf._set_permission_context(
        read_paths=[str(sandbox / "docs")],
        write_paths=[],
    )
    with pytest.raises(PermissionError):
        sf.open(str(sandbox / "docs" / "a.md"), "w")


def test_open_append_mode_uses_write_gate(sandbox: Path) -> None:
    """Tier 2: ``a`` (append) is a write — must be in write_paths."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "log.txt"
    with sf.open(str(target), "a") as f:
        f.write("entry\n")
    assert target.read_text() == "entry\n"


def test_open_read_plus_mode_requires_write(sandbox: Path) -> None:
    """Tier 2: ``r+`` opens for read+write — the '+' flag elevates to
    the write gate. A path in read_paths but NOT write_paths fails."""
    target = sandbox / "docs" / "a.md"
    sf._set_permission_context(
        read_paths=[str(sandbox / "docs")],
        write_paths=[],
    )
    with pytest.raises(PermissionError):
        sf.open(str(target), "r+")


# ---------------------------------------------------------------------------
# Context override / re-initialisation
# ---------------------------------------------------------------------------


def test_context_can_be_reinitialised(sandbox: Path) -> None:
    """Tier 2: calling ``_set_permission_context`` again overrides the
    previous context. Production use-case: a tester sets up paths,
    runs a step, then resets to a different set for the next step.
    """
    sf._set_permission_context(read_paths=[str(sandbox / "docs")])
    assert sf.read(str(sandbox / "docs" / "a.md")) == "hello A\n"

    sf._set_permission_context(read_paths=[])
    with pytest.raises(PermissionError):
        sf.read(str(sandbox / "docs" / "a.md"))


# ---------------------------------------------------------------------------
# mkdir — FP-0042 Phase 2.2
# ---------------------------------------------------------------------------


def test_mkdir_within_write_path_creates_directory(sandbox: Path) -> None:
    """Tier 2: mkdir in declared write zone creates the directory."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "new"
    sf.mkdir(str(target))
    assert target.is_dir()


def test_mkdir_outside_write_path_raises(sandbox: Path) -> None:
    """Tier 2: mkdir outside the declared write zone raises PermissionError."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    with pytest.raises(PermissionError):
        sf.mkdir(str(sandbox / "docs" / "should_not_exist"))


def test_mkdir_without_context_raises() -> None:
    """Tier 2: mkdir without permission context raises clearly."""
    with pytest.raises(PermissionError):
        sf.mkdir("/tmp/no-context-mkdir")


def test_mkdir_existing_dir_default_raises(sandbox: Path) -> None:
    """Tier 2: mkdir raises FileExistsError on existing dir by default."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "already"
    target.mkdir()
    with pytest.raises(FileExistsError):
        sf.mkdir(str(target))


def test_mkdir_exist_ok_true_swallows_existing(sandbox: Path) -> None:
    """Tier 2: mkdir(exist_ok=True) is a no-op on existing dir."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "already"
    target.mkdir()
    sf.mkdir(str(target), exist_ok=True)
    assert target.is_dir()


def test_mkdir_parents_true_creates_intermediates(sandbox: Path) -> None:
    """Tier 2: mkdir(parents=True) creates intermediate directories."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "a" / "b" / "c"
    sf.mkdir(str(target), parents=True)
    assert target.is_dir()


def test_mkdir_parents_false_missing_intermediate_raises(sandbox: Path) -> None:
    """Tier 2: mkdir without parents=True fails when intermediates missing."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "missing" / "leaf"
    with pytest.raises(FileNotFoundError):
        sf.mkdir(str(target))


# ---------------------------------------------------------------------------
# delete — FP-0042 Phase 2.2
# ---------------------------------------------------------------------------


def test_delete_within_write_path_removes_file(sandbox: Path) -> None:
    """Tier 2: delete in declared write zone removes the file."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "trash.txt"
    target.write_text("bye\n", encoding="utf-8")
    sf.delete(str(target))
    assert not target.exists()


def test_delete_outside_write_path_raises(sandbox: Path) -> None:
    """Tier 2: delete outside the declared write zone raises PermissionError;
    the file stays put."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "docs" / "a.md"
    with pytest.raises(PermissionError):
        sf.delete(str(target))
    assert target.exists()


def test_delete_missing_default_raises(sandbox: Path) -> None:
    """Tier 2: delete of a missing file raises FileNotFoundError by default."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    with pytest.raises(FileNotFoundError):
        sf.delete(str(sandbox / "out" / "ghost.txt"))


def test_delete_missing_ok_true_swallows(sandbox: Path) -> None:
    """Tier 2: delete(missing_ok=True) is a no-op when the file doesn't exist."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    sf.delete(str(sandbox / "out" / "ghost.txt"), missing_ok=True)


def test_delete_without_context_raises() -> None:
    """Tier 2: delete without permission context raises clearly."""
    with pytest.raises(PermissionError):
        sf.delete("/tmp/no-context-delete")


# ---------------------------------------------------------------------------
# write_atomic — FP-0042 Phase 2.3
# ---------------------------------------------------------------------------


def test_write_atomic_within_write_path_writes_content(sandbox: Path) -> None:
    """Tier 2: write_atomic in declared write zone lands the content."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "atomic.txt"
    sf.write_atomic(str(target), "atomic payload\n")
    assert target.read_text() == "atomic payload\n"


def test_write_atomic_outside_write_path_raises(sandbox: Path) -> None:
    """Tier 2: write_atomic outside the write zone raises; nothing changes."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "docs" / "a.md"
    with pytest.raises(PermissionError):
        sf.write_atomic(str(target), "should not land")
    assert target.read_text() == "hello A\n"


def test_write_atomic_replaces_existing(sandbox: Path) -> None:
    """Tier 2: write_atomic overwrites an existing file in-place atomically."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "data.txt"
    target.write_text("original\n", encoding="utf-8")
    sf.write_atomic(str(target), "replacement\n")
    assert target.read_text() == "replacement\n"


def test_write_atomic_without_context_raises() -> None:
    """Tier 2: write_atomic without permission context raises clearly."""
    with pytest.raises(PermissionError):
        sf.write_atomic("/tmp/no-context-atomic", "x")


def test_write_atomic_leaves_no_temp_residue(sandbox: Path) -> None:
    """Tier 2: a successful write_atomic leaves no .reyn_safe_atomic_* temp
    file behind in the destination directory."""
    sf._set_permission_context(write_paths=[str(sandbox / "out")])
    target = sandbox / "out" / "clean.txt"
    sf.write_atomic(str(target), "payload\n")
    # No temp file with our prefix should remain.
    siblings = list((sandbox / "out").glob(".reyn_safe_atomic_*"))
    assert siblings == [], f"temp residue: {siblings}"
