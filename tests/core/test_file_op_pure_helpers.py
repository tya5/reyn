"""Tier 2: core/op_runtime/file.py pure helper contracts.

_image_mime_for_path(path) maps a file path to its image MIME type by
extension, or None for non-image paths.

_changed_region_preview(new_content, start_offset, new_len, ...) renders
a bounded, numbered-line view of the region surrounding an edit change.
"""
from __future__ import annotations

import pytest

from reyn.core.op_runtime.file import _changed_region_preview, _image_mime_for_path

# ── _image_mime_for_path ──────────────────────────────────────────────────────


def test_image_mime_png() -> None:
    """Tier 2: .png extension → 'image/png'."""
    assert _image_mime_for_path("photo.png") == "image/png"


def test_image_mime_jpg() -> None:
    """Tier 2: .jpg extension → 'image/jpeg'."""
    assert _image_mime_for_path("photo.jpg") == "image/jpeg"


def test_image_mime_jpeg() -> None:
    """Tier 2: .jpeg extension → 'image/jpeg' (same MIME as .jpg)."""
    assert _image_mime_for_path("photo.jpeg") == "image/jpeg"


def test_image_mime_gif() -> None:
    """Tier 2: .gif extension → 'image/gif'."""
    assert _image_mime_for_path("photo.gif") == "image/gif"


def test_image_mime_webp() -> None:
    """Tier 2: .webp extension → 'image/webp'."""
    assert _image_mime_for_path("photo.webp") == "image/webp"


def test_image_mime_svg() -> None:
    """Tier 2: .svg extension → 'image/svg+xml'."""
    assert _image_mime_for_path("diagram.svg") == "image/svg+xml"


def test_image_mime_uppercase_extension() -> None:
    """Tier 2: uppercase extension is normalised; .PNG → 'image/png'."""
    assert _image_mime_for_path("photo.PNG") == "image/png"


def test_image_mime_non_image_extension_returns_none() -> None:
    """Tier 2: non-image extension → None (treat as text)."""
    assert _image_mime_for_path("script.py") is None


def test_image_mime_no_extension_returns_none() -> None:
    """Tier 2: path with no '.' → None."""
    assert _image_mime_for_path("README") is None


def test_image_mime_nested_path_uses_only_extension() -> None:
    """Tier 2: directory components do not affect the MIME lookup."""
    assert _image_mime_for_path("assets/images/logo.png") == "image/png"
    assert _image_mime_for_path("docs/notes.txt") is None


# ── _changed_region_preview ───────────────────────────────────────────────────


def _content_with_change(before: list[str], change: str, after: list[str]) -> tuple[str, int, int]:
    """Return (full_content, start_offset, change_len) for building preview inputs."""
    prefix = "\n".join(before) + ("\n" if before else "")
    start = len(prefix)
    full = prefix + change + ("\n" + "\n".join(after) if after else "")
    return full, start, len(change)


def test_changed_region_preview_shows_changed_line_number() -> None:
    """Tier 2: the changed line appears with its correct 1-based line number."""
    lines_before = ["alpha", "beta", "gamma"]
    content, offset, length = _content_with_change(lines_before, "DELTA", ["epsilon"])
    result = _changed_region_preview(content, offset, length, context_lines=0)
    assert "4\tDELTA" in result


def test_changed_region_preview_includes_context_lines() -> None:
    """Tier 2: context_lines surrounds the changed line with neighbouring lines."""
    lines_before = ["a", "b", "c"]
    content, offset, length = _content_with_change(lines_before, "X", ["d", "e"])
    result = _changed_region_preview(content, offset, length, context_lines=1)
    assert "3\tc" in result   # one line before
    assert "4\tX" in result   # the change
    assert "5\td" in result   # one line after


def test_changed_region_preview_first_line() -> None:
    """Tier 2: change at the very start of content does not underflow (lo >= 0)."""
    content = "NEW\nline2\nline3"
    result = _changed_region_preview(content, 0, len("NEW"), context_lines=1)
    assert "1\tNEW" in result


def test_changed_region_preview_truncation_marker() -> None:
    """Tier 2: when the region exceeds max_lines, the truncation suffix is appended."""
    big = "\n".join(f"line{i}" for i in range(100))
    offset = big.index("line50")
    result = _changed_region_preview(big, offset, len("line50"), max_lines=3)
    assert "…\t(preview truncated)" in result


def test_changed_region_preview_no_truncation_within_limit() -> None:
    """Tier 2: region within max_lines does not include the truncation marker."""
    content = "a\nb\nc\nd\ne"
    offset = content.index("c")
    result = _changed_region_preview(content, offset, len("c"), context_lines=1, max_lines=40)
    assert "…" not in result


def test_changed_region_preview_deletion_shows_context() -> None:
    """Tier 2: deletion (new_len=0) renders surrounding context at the seam."""
    content = "a\nb\nc\nd\ne"
    offset = content.index("c")
    result = _changed_region_preview(content, offset, 0, context_lines=1)
    assert "2\tb" in result
    assert "3\tc" in result
    assert "4\td" in result
