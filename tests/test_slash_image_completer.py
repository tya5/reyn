"""Tier 2: ``_image_path_completer`` surfaces filesystem paths for the TUI picker.

When the user types ``/image <path-partial>`` the picker calls
``cmd.completer(session, arg_partial)`` via ``InputBar._run_completer``.
This file pins the contract that the completer:
  - returns image files (.png / .jpg / .jpeg / .gif / .webp / .svg)
    in the requested directory
  - appends a trailing ``/`` to directory entries so the user can keep
    navigating
  - excludes non-image files (e.g. .txt, .py)
  - returns ``[]`` for a bad / non-existent path
  - returns ``[]`` on any OS error (never breaks the picker)
  - accepts ``session`` for the CompleterFn contract but does not use it
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.slash.image import _image_path_completer


class _FakeSession:
    """Minimal stub — completer doesn't use the session, but the contract
    requires it as the first argument."""
    pass


# ── happy-path: image files are returned ─────────────────────────────────────


def test_image_completer_returns_image_file(tmp_path: Path) -> None:
    """Tier 2: a directory with an image file returns that file in completions."""
    (tmp_path / "shot.png").write_bytes(b"")
    results = _image_path_completer(_FakeSession(), str(tmp_path) + "/")
    assert any("shot.png" in r for r in results), (
        f"expected shot.png in completions; got {results}"
    )


def test_image_completer_excludes_non_image_files(tmp_path: Path) -> None:
    """Tier 2: non-image files (.txt, .py) are excluded from completions."""
    (tmp_path / "readme.txt").write_bytes(b"")
    (tmp_path / "script.py").write_bytes(b"")
    (tmp_path / "photo.jpg").write_bytes(b"")
    results = _image_path_completer(_FakeSession(), str(tmp_path) + "/")
    assert all(".txt" not in r and ".py" not in r for r in results), (
        f"non-image files leaked into completions: {results}"
    )
    assert any("photo.jpg" in r for r in results), (
        f"expected photo.jpg in completions; got {results}"
    )


def test_image_completer_includes_directories_with_trailing_slash(tmp_path: Path) -> None:
    """Tier 2: subdirectories are returned with a trailing slash."""
    sub = tmp_path / "subdir"
    sub.mkdir()
    results = _image_path_completer(_FakeSession(), str(tmp_path) + "/")
    dir_entries = [r for r in results if r.endswith("/")]
    assert any("subdir" in r for r in dir_entries), (
        f"expected subdir/ in completions; got {results}"
    )


def test_image_completer_bad_path_returns_empty() -> None:
    """Tier 2: a non-existent directory returns [] instead of raising."""
    result = _image_path_completer(_FakeSession(), "/this/path/does/not/exist/")
    assert result == [], f"expected [] for bad path, got {result}"


def test_image_completer_all_image_extensions_accepted(tmp_path: Path) -> None:
    """Tier 2: all six supported extensions are included (.png .jpg .jpeg .gif .webp .svg)."""
    extensions = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]
    for ext in extensions:
        (tmp_path / f"file{ext}").write_bytes(b"")
    results = _image_path_completer(_FakeSession(), str(tmp_path) + "/")
    for ext in extensions:
        assert any(ext in r for r in results), (
            f"extension {ext} not in completions: {results}"
        )


def test_image_completer_case_insensitive_extensions(tmp_path: Path) -> None:
    """Tier 2: upper-case extensions (.PNG, .JPG) are accepted."""
    (tmp_path / "UPPER.PNG").write_bytes(b"")
    (tmp_path / "Mixed.Jpg").write_bytes(b"")
    results = _image_path_completer(_FakeSession(), str(tmp_path) + "/")
    assert any("UPPER.PNG" in r for r in results), (
        f"upper-case .PNG not matched: {results}"
    )
    assert any("Mixed.Jpg" in r for r in results), (
        f"mixed-case .Jpg not matched: {results}"
    )


def test_image_completer_prefix_filtering(tmp_path: Path) -> None:
    """Tier 2: only entries matching the typed prefix are returned."""
    (tmp_path / "alpha.png").write_bytes(b"")
    (tmp_path / "beta.png").write_bytes(b"")
    # The completer receives the directory + the partial prefix.  The
    # _run_completer layer does the final startswith filter, but the
    # completer itself also applies the prefix when iterating.
    prefix = str(tmp_path) + "/al"
    results = _image_path_completer(_FakeSession(), prefix)
    assert any("alpha.png" in r for r in results), (
        f"expected alpha.png; got {results}"
    )
    assert not any("beta.png" in r for r in results), (
        f"beta.png should be filtered by prefix 'al'; got {results}"
    )


def test_image_completer_bounded_at_max(tmp_path: Path) -> None:
    """Tier 2: result count is capped at the module constant (default 20)."""
    from reyn.chat.slash.image import _COMPLETER_MAX
    for i in range(_COMPLETER_MAX + 10):
        (tmp_path / f"img{i:03d}.png").write_bytes(b"")
    results = _image_path_completer(_FakeSession(), str(tmp_path) + "/")
    assert len(results) <= _COMPLETER_MAX, (
        f"completer returned more than {_COMPLETER_MAX} results: {len(results)}"
    )


def test_image_completer_session_unused(tmp_path: Path) -> None:
    """Tier 2: the session argument is accepted but never read — passing None
    must not crash (completer is session-independent)."""
    (tmp_path / "img.png").write_bytes(b"")
    # Pass None explicitly to verify the contract holds.
    result = _image_path_completer(None, str(tmp_path) + "/")  # type: ignore[arg-type]
    assert any("img.png" in r for r in result)
