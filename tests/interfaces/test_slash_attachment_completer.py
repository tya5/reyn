"""Tier 2: ``_attachment_path_completer`` surfaces filesystem paths for
the TUI picker.

Mirrors ``test_slash_image_completer.py``'s own established contract,
narrowed to what genuinely DIFFERS: this completer accepts every regular
file (any extension), unlike ``/image``'s own image-only extension
filter. When the user types ``/attachment <path-partial>`` the picker
calls ``cmd.completer(session, arg_partial)`` via
``InputBar._run_completer``. This file pins:
  - EVERY regular file is returned (not just image extensions).
  - Directory entries still get a trailing ``/``.
  - Bad path / OS error still returns ``[]``.
  - ``session`` is accepted but unused.
  - The same ``_COMPLETER_MAX`` bound applies.
"""
from __future__ import annotations

import sys
from pathlib import Path

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.slash.attachment import _attachment_path_completer


class _FakeSession:
    """Minimal stub — completer doesn't use the session, but the contract
    requires it as the first argument."""
    pass


def test_attachment_completer_returns_non_image_file(tmp_path: Path) -> None:
    """Tier 2: the #5509 design point — a non-image file (.pdf) is
    returned, unlike /image's own completer which would exclude it."""
    (tmp_path / "report.pdf").write_bytes(b"")
    results = _attachment_path_completer(_FakeSession(), str(tmp_path) + "/")
    assert any("report.pdf" in r for r in results), (
        f"expected report.pdf in completions; got {results}"
    )


def test_attachment_completer_returns_every_extension_including_none(tmp_path: Path) -> None:
    """Tier 2: unlike /image's closed extension set, EVERY regular file
    is a candidate — including one with no extension at all."""
    (tmp_path / "readme.txt").write_bytes(b"")
    (tmp_path / "script.py").write_bytes(b"")
    (tmp_path / "photo.jpg").write_bytes(b"")
    (tmp_path / "Makefile").write_bytes(b"")
    results = _attachment_path_completer(_FakeSession(), str(tmp_path) + "/")
    for name in ("readme.txt", "script.py", "photo.jpg", "Makefile"):
        assert any(name in r for r in results), f"expected {name} in completions; got {results}"


def test_attachment_completer_includes_directories_with_trailing_slash(tmp_path: Path) -> None:
    """Tier 2: subdirectories are returned with a trailing slash — same
    contract as /image's own completer."""
    sub = tmp_path / "subdir"
    sub.mkdir()
    results = _attachment_path_completer(_FakeSession(), str(tmp_path) + "/")
    dir_entries = [r for r in results if r.endswith("/")]
    assert any("subdir" in r for r in dir_entries), (
        f"expected subdir/ in completions; got {results}"
    )


def test_attachment_completer_bad_path_returns_empty() -> None:
    """Tier 2: a non-existent directory returns [] instead of raising."""
    result = _attachment_path_completer(_FakeSession(), "/this/path/does/not/exist/")
    assert result == [], f"expected [] for bad path, got {result}"


def test_attachment_completer_prefix_filtering(tmp_path: Path) -> None:
    """Tier 2: only entries matching the typed prefix are returned."""
    (tmp_path / "alpha.pdf").write_bytes(b"")
    (tmp_path / "beta.pdf").write_bytes(b"")
    prefix = str(tmp_path) + "/al"
    results = _attachment_path_completer(_FakeSession(), prefix)
    assert any("alpha.pdf" in r for r in results), f"expected alpha.pdf; got {results}"
    assert not any("beta.pdf" in r for r in results), (
        f"beta.pdf should be filtered by prefix 'al'; got {results}"
    )


def test_attachment_completer_bounded_at_max(tmp_path: Path) -> None:
    """Tier 2: result count is capped at the module constant (default 20) —
    same bound as /image's own completer."""
    from reyn.interfaces.slash.attachment import _COMPLETER_MAX
    for i in range(_COMPLETER_MAX + 10):
        (tmp_path / f"file{i:03d}.pdf").write_bytes(b"")
    results = _attachment_path_completer(_FakeSession(), str(tmp_path) + "/")
    assert len(results) <= _COMPLETER_MAX, (
        f"completer returned more than {_COMPLETER_MAX} results: {len(results)}"
    )


def test_attachment_completer_session_unused(tmp_path: Path) -> None:
    """Tier 2: the session argument is accepted but never read — passing
    None must not crash (completer is session-independent)."""
    (tmp_path / "any.pdf").write_bytes(b"")
    result = _attachment_path_completer(None, str(tmp_path) + "/")  # type: ignore[arg-type]
    assert any("any.pdf" in r for r in result)
