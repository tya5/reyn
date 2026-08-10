"""Tier 2: data/workspace/media_store.py pure helper contracts.

_ext_for_mime(mime) maps a MIME type string to a file extension, stripping
any '; charset=...' suffix. Returns '' for unknown types.

_safe_token(value) sanitises a string for embedding in a filename, replacing
path-separators, spaces, and other shell-unfriendly characters with '_'.
"""
from __future__ import annotations

from reyn.data.workspace.media_store import _ext_for_mime, _safe_token

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
