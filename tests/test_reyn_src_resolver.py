"""Tier 2: ``reyn_src_*`` resolver pins safety + listing semantics.

The resolver backs the chat router's ``reyn_src_list`` / ``reyn_src_read``
tools (= "agent navigation of Reyn's own repo"). Tests pin:

- repo-root resolution finds the actual Reyn repo (= dev install).
- path traversal (``..``) is refused, not silently leaked outside root.
- absent-target reports an error rather than crashing.
- ``list_entries`` output has the documented shape and excludes the
  noise directories (.git, __pycache__, etc.).
- ``read_text`` returns content for valid text files and rejects
  binaries / oversized files / directories.

Tier 2 because these are OS invariants the chat router depends on; an
escape past the root would be a real safety regression.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.reyn_src import (
    list_entries,
    read_text,
    resolve_reyn_root,
    safe_resolve_inside,
)

# ── repo-root resolution ────────────────────────────────────────────────────


def test_resolve_reyn_root_finds_repo_in_dev_install():
    """Tier 2: dev-install resolves the repo root via pyproject.toml.

    Verifies the running test setup IS a Reyn dev install (= our own
    pyproject lives at the resolved root). If this trips, the resolver
    or the install layout has drifted.
    """
    # Cache from prior tests may interfere — re-resolve to be sure.
    resolve_reyn_root.cache_clear()
    root = resolve_reyn_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "reyn").is_dir()
    # README at the resolved root (= what `reyn_src_read("README.md")`
    # would surface to the agent).
    assert (root / "README.md").is_file()


# ── safety: path traversal ──────────────────────────────────────────────────


def test_safe_resolve_inside_blocks_dotdot_escape():
    """Tier 2: ``..`` that escapes ``root`` is refused, regardless of
    where the ``..`` segments appear in the path.

    Pins the safety boundary against:

      - leading ``..`` (= ``../etc/passwd``)
      - middle ``..`` (= ``src/reyn/../../../etc/passwd``)
      - trailing ``..`` after deep prefix (= ``docs/concepts/../../../../etc/passwd``)
      - multi-bounce escape (= ``src/../../tmp/x``)

    The protection mechanism (= ``Path.resolve()`` canonicalises every
    ``..`` then ``relative_to(root)`` rejects out-of-root targets) covers
    all positions; this test makes that contract explicit so a future
    refactor can't silently weaken it to "leading only".
    """
    root = resolve_reyn_root()
    escapes = [
        "../etc/passwd",
        "src/reyn/../../../etc/passwd",
        "docs/concepts/../../../../etc/passwd",
        "src/../../tmp/x",
        "docs/../../etc/foo",
    ]
    for path in escapes:
        with pytest.raises(ValueError, match="outside"):
            safe_resolve_inside(root, path)


def test_safe_resolve_inside_allows_internal_dotdot():
    """Tier 2: ``..`` is OK as long as the resolved target stays inside.

    ``src/reyn/../README.md`` resolves to ``<root>/README.md``, which
    is inside ``root``, so it's permitted. The escape check is on the
    resolved target, not the literal path string.
    """
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "src/reyn/../../README.md")
    assert target == (root / "README.md").resolve()


def test_safe_resolve_inside_handles_leading_slash():
    """Tier 2: ``"/README.md"`` (leading slash from confused LLM) is
    treated as repo-root-relative, not as an absolute filesystem path.
    """
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "/README.md")
    assert target == (root / "README.md").resolve()


def test_safe_resolve_inside_empty_path_is_root():
    """Tier 2: ``""`` resolves to the repo root itself (= the documented
    "list the top level" semantic).
    """
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "")
    assert target == root.resolve()


def test_safe_resolve_inside_missing_path_errors():
    """Tier 2: requesting a non-existent path returns ValueError, not
    a silent empty result. The chat tool surfaces this as a structured
    error so the LLM can correct.
    """
    root = resolve_reyn_root()
    with pytest.raises(ValueError, match="does not exist"):
        safe_resolve_inside(root, "no-such-file.xyz")


# ── list_entries ─────────────────────────────────────────────────────────────


def test_list_entries_root_lists_top_level():
    """Tier 2: listing ``""`` returns the repo top-level with README.md
    and src/ visible, but excludes noise dirs (.git, __pycache__).
    """
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "")
    result = list_entries(root, target, "")
    assert "entries" in result
    names = {e["name"] for e in result["entries"]}
    # Must include known top-level content.
    assert "README.md" in names
    assert "src" in names
    # Must exclude noise (= the user can verify these on disk if needed
    # but they don't help the LLM understand Reyn).
    for noisy in {".git", "__pycache__", ".pytest_cache", "venv"}:
        assert noisy not in names, f"{noisy!r} leaked into listing"
    # File / dir distinction is reported.
    by_name = {e["name"]: e["type"] for e in result["entries"]}
    assert by_name["src"] == "dir"
    assert by_name["README.md"] == "file"


def test_list_entries_subdir():
    """Tier 2: listing ``"src/reyn/chat"`` returns the chat layer files
    (= router_loop.py, session.py present)."""
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "src/reyn/chat")
    result = list_entries(root, target, "src/reyn/chat")
    names = {e["name"] for e in result["entries"]}
    assert "router_loop.py" in names
    assert "session.py" in names
    assert result["path"] == "src/reyn/chat"


def test_list_entries_on_file_returns_error():
    """Tier 2: list-on-file is an error (= LLM should call read instead).
    The error message points to the alternative tool.
    """
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "README.md")
    result = list_entries(root, target, "README.md")
    assert "error" in result
    assert "reyn_src_read" in result["error"]


# ── read_text ───────────────────────────────────────────────────────────────


def test_read_text_returns_readme_content():
    """Tier 2: reading ``"README.md"`` returns its body and the path
    that the LLM can echo back to the user."""
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "README.md")
    result = read_text(target, "README.md")
    assert result["path"] == "README.md"
    assert "content" in result
    # Sanity: README contains the project name.
    assert "Reyn" in result["content"] or "reyn" in result["content"]


def test_read_text_on_directory_returns_error():
    """Tier 2: read-on-directory is an error pointing at reyn_src_list."""
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "src")
    result = read_text(target, "src")
    assert "error" in result
    assert "reyn_src_list" in result["error"]


def test_read_text_oversize_file_rejected(tmp_path: Path):
    """Tier 2: a file larger than the per-call cap returns an error
    instead of blowing up the LLM context.

    Synthesizes a fake oversized file under tmp_path and confirms the
    cap message surfaces. Doesn't need the real Reyn root.
    """
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (300 * 1024))  # 300 KB > 256 KB cap
    result = read_text(big, "big.txt")
    assert "error" in result
    assert "larger" in result["error"].lower() or "cap" in result["error"].lower()


def test_read_text_binary_file_rejected(tmp_path: Path):
    """Tier 2: a non-UTF-8 file returns a structured error rather than
    a Unicode exception bubbling up.
    """
    binfile = tmp_path / "bin.dat"
    binfile.write_bytes(b"\xff\xfe\x00\x01\x02")
    result = read_text(binfile, "bin.dat")
    assert "error" in result
    assert "UTF-8" in result["error"] or "text" in result["error"].lower()
