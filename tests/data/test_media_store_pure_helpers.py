"""Tier 2: data/workspace/media_store.py pure helper contracts.

_ext_for_mime(mime) maps a MIME type string to a file extension, stripping
any '; charset=...' suffix. Returns '' for unknown types.

_safe_token(value) sanitises a string for embedding in a filename, replacing
path-separators, spaces, and other shell-unfriendly characters with '_'.

_dir_stats(directory) is (file_count, total_bytes) for a flat directory —
see its own #4671 census tests below for the narrowed-except behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.data.workspace.media_store import _dir_stats, _ext_for_mime, _safe_token

# ── _ext_for_mime ─────────────────────────────────────────────────────────────


def test_ext_for_mime_image_png() -> None:
    """Tier 2: 'image/png' → '.png'."""
    assert _ext_for_mime("image/png") == ".png"


def test_ext_for_mime_image_jpeg() -> None:
    """Tier 2: 'image/jpeg' → '.jpg'."""
    assert _ext_for_mime("image/jpeg") == ".jpg"


def test_ext_for_mime_image_gif() -> None:
    """Tier 2: 'image/gif' → '.gif'."""
    assert _ext_for_mime("image/gif") == ".gif"


def test_ext_for_mime_image_webp() -> None:
    """Tier 2: 'image/webp' → '.webp'."""
    assert _ext_for_mime("image/webp") == ".webp"


def test_ext_for_mime_text_plain() -> None:
    """Tier 2: 'text/plain' → '.txt'."""
    assert _ext_for_mime("text/plain") == ".txt"


def test_ext_for_mime_application_json() -> None:
    """Tier 2: 'application/json' → '.json'."""
    assert _ext_for_mime("application/json") == ".json"


def test_ext_for_mime_strips_charset_suffix() -> None:
    """Tier 2: '; charset=utf-8' suffix is stripped before lookup."""
    assert _ext_for_mime("text/plain; charset=utf-8") == ".txt"
    assert _ext_for_mime("application/json; charset=utf-8") == ".json"


def test_ext_for_mime_case_insensitive() -> None:
    """Tier 2: MIME type is lowercased before lookup ('image/PNG' → '.png')."""
    assert _ext_for_mime("image/PNG") == ".png"
    assert _ext_for_mime("TEXT/PLAIN") == ".txt"


def test_ext_for_mime_unknown_returns_empty() -> None:
    """Tier 2: unknown MIME type returns '' (caller writes without extension hint)."""
    assert _ext_for_mime("video/mp4") == ""
    assert _ext_for_mime("application/octet-stream") == ""


def test_ext_for_mime_empty_returns_empty() -> None:
    """Tier 2: empty string returns ''."""
    assert _ext_for_mime("") == ""


# ── _safe_token ───────────────────────────────────────────────────────────────


def test_safe_token_alnum_passthrough() -> None:
    """Tier 2: alphanumeric characters pass through unchanged."""
    assert _safe_token("model123") == "model123"


def test_safe_token_underscore_hyphen_dot_kept() -> None:
    """Tier 2: '_', '-', and '.' are kept as-is."""
    assert _safe_token("claude-3.5") == "claude-3.5"
    assert _safe_token("a_b_c") == "a_b_c"


def test_safe_token_space_replaced() -> None:
    """Tier 2: space is replaced with '_'."""
    assert _safe_token("has space") == "has_space"


def test_safe_token_slash_replaced() -> None:
    """Tier 2: path separator '/' is replaced with '_'."""
    assert _safe_token("path/to/file") == "path_to_file"


def test_safe_token_special_chars_replaced() -> None:
    """Tier 2: shell-unfriendly characters (@, #, !) are each replaced with '_'."""
    assert _safe_token("a@b#c!") == "a_b_c_"


def test_safe_token_empty_returns_empty() -> None:
    """Tier 2: empty string returns empty string."""
    assert _safe_token("") == ""


# ── _dir_stats ───────────────────────────────────────────────────────────


def test_dir_stats_missing_directory_reports_zero(tmp_path: Path) -> None:
    """Tier 2: no directory at all — (0, 0), not an error."""
    assert _dir_stats(tmp_path / "does-not-exist") == (0, 0)


def test_dir_stats_counts_files_and_bytes(tmp_path: Path) -> None:
    """Tier 2: real files on disk — count and byte total match exactly."""
    (tmp_path / "a.bin").write_bytes(b"x" * 10)
    (tmp_path / "b.bin").write_bytes(b"y" * 25)
    assert _dir_stats(tmp_path) == (2, 35)


def test_dir_stats_file_vanishing_mid_scan_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: #4671 — a file that disappears between ``iterdir()`` listing
    it and this function's own ``stat()`` call (a concurrent delete) is
    skipped, not raised — the existing, intentional best-effort behavior,
    now scoped to ``FileNotFoundError`` specifically rather than a blanket
    ``OSError`` (see the next test for why that distinction matters)."""
    survivor = tmp_path / "survivor.bin"
    vanishing = tmp_path / "vanishing.bin"
    survivor.write_bytes(b"x" * 10)
    vanishing.write_bytes(b"y" * 25)

    real_stat = Path.stat

    def _stat_raises_for_vanishing(self, *args, **kwargs):
        if self == vanishing:
            raise FileNotFoundError(f"simulated race: {self} vanished mid-scan")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat_raises_for_vanishing)

    assert _dir_stats(tmp_path) == (1, 10)


def test_dir_stats_permission_error_is_not_swallowed(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: #4671 — only ``FileNotFoundError`` is treated as "vanished,
    skip silently". A ``PermissionError`` on one file must propagate, not
    be silently absorbed as "this file doesn't count" — swallowing it
    would under-report the directory's real footprint with no disclosure
    that anything was skipped (D-1: measure, don't fake)."""
    blocked = tmp_path / "blocked.bin"
    blocked.write_bytes(b"z" * 5)

    real_stat = Path.stat

    def _stat_raises_permission_error(self, *args, **kwargs):
        if self == blocked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat_raises_permission_error)

    with pytest.raises(PermissionError):
        _dir_stats(tmp_path)
