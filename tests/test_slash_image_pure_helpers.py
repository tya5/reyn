"""Tier 2: /image slash — _mime_for_path + _file_size_human pure helper contracts.

`_mime_for_path` maps image extensions to MIME types; `_file_size_human` formats
a byte count as a human-readable string.  Both are used in the /image handler and
its progress display.  Pinning them prevents a silent rename of the extension map
or a threshold change from breaking multimodal support.
"""
from __future__ import annotations

from pathlib import Path

from reyn.interfaces.slash.image import _file_size_human, _mime_for_path

# ── _mime_for_path ─────────────────────────────────────────────────────────


def test_mime_png() -> None:
    """Tier 2: .png → 'image/png'."""
    assert _mime_for_path(Path("shot.png")) == "image/png"


def test_mime_jpg() -> None:
    """Tier 2: .jpg → 'image/jpeg'."""
    assert _mime_for_path(Path("photo.jpg")) == "image/jpeg"


def test_mime_jpeg_alias() -> None:
    """Tier 2: .jpeg → 'image/jpeg' (same MIME as .jpg)."""
    assert _mime_for_path(Path("photo.jpeg")) == "image/jpeg"


def test_mime_gif() -> None:
    """Tier 2: .gif → 'image/gif'."""
    assert _mime_for_path(Path("anim.gif")) == "image/gif"


def test_mime_webp() -> None:
    """Tier 2: .webp → 'image/webp'."""
    assert _mime_for_path(Path("image.webp")) == "image/webp"


def test_mime_svg() -> None:
    """Tier 2: .svg → 'image/svg+xml'."""
    assert _mime_for_path(Path("icon.svg")) == "image/svg+xml"


def test_mime_case_insensitive_upper() -> None:
    """Tier 2: .PNG (upper-case) is accepted (case-insensitive lookup)."""
    assert _mime_for_path(Path("SHOT.PNG")) == "image/png"


def test_mime_case_insensitive_mixed() -> None:
    """Tier 2: .Jpg (mixed case) is accepted."""
    assert _mime_for_path(Path("photo.Jpg")) == "image/jpeg"


def test_mime_unsupported_extension_returns_none() -> None:
    """Tier 2: .txt returns None (unsupported → caller shows error)."""
    assert _mime_for_path(Path("notes.txt")) is None


def test_mime_no_extension_returns_none() -> None:
    """Tier 2: a path with no extension returns None."""
    assert _mime_for_path(Path("Makefile")) is None


# ── _file_size_human ───────────────────────────────────────────────────────


def test_size_human_bytes_range() -> None:
    """Tier 2: values < 1000 show as '… bytes'."""
    assert "bytes" in _file_size_human(0)
    assert "bytes" in _file_size_human(999)


def test_size_human_exact_1000_is_kb() -> None:
    """Tier 2: exactly 1000 bytes crosses into KB display."""
    out = _file_size_human(1000)
    assert "KB" in out


def test_size_human_kb_range() -> None:
    """Tier 2: 1000–999999 bytes display as '…KB'."""
    assert "KB" in _file_size_human(1_500)
    assert "KB" in _file_size_human(999_999)


def test_size_human_exact_1mb_is_mb() -> None:
    """Tier 2: exactly 1_000_000 bytes crosses into MB display."""
    out = _file_size_human(1_000_000)
    assert "MB" in out


def test_size_human_mb_range() -> None:
    """Tier 2: values ≥ 1 000 000 display as '…MB'."""
    assert "MB" in _file_size_human(2_500_000)
