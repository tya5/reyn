"""Tier 2: pure helpers in runtime/router_loop.py.

``_strip_frontmatter(content)``       — strip ---fm--- block from memory file text
``_overflow_ref_text(ref)``           — format image-overflow reference message
``_is_context_overflow_error(exc)``   — keyword-match context length errors
``_is_unsupported_param_error(exc)``  — class-name/keyword unsupported param errors
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.runtime.router_loop import (
    _is_context_overflow_error,
    _is_unsupported_param_error,
    _overflow_ref_text,
    _strip_frontmatter,
)

# ---------------------------------------------------------------------------
# _strip_frontmatter
# ---------------------------------------------------------------------------


def test_strip_frontmatter_removes_fm_block() -> None:
    """Tier 2: standard ---frontmatter--- block is stripped; body returned."""
    text = "---\nname: foo\ndescription: bar\n---\n\nActual content here."
    result = _strip_frontmatter(text)
    assert "name:" not in result
    assert "Actual content here." in result


def test_strip_frontmatter_no_fm_passthrough() -> None:
    """Tier 2: text without opening '---' is returned unchanged."""
    text = "Just the body, no frontmatter."
    assert _strip_frontmatter(text) == text


def test_strip_frontmatter_unclosed_passthrough() -> None:
    """Tier 2: opening '---' with no closing '---' → unchanged (no truncation)."""
    text = "---\nname: foo\nno close"
    assert _strip_frontmatter(text) == text


def test_strip_frontmatter_empty_string() -> None:
    """Tier 2: empty string → empty string (no crash)."""
    assert _strip_frontmatter("") == ""


def test_strip_frontmatter_none_passthrough() -> None:
    """Tier 2: None → empty string (content or '' fallback)."""
    result = _strip_frontmatter(None)  # type: ignore[arg-type]
    assert result == ""


def test_strip_frontmatter_body_only_no_leading_blank() -> None:
    """Tier 2: single leading blank line after closing '---' is trimmed."""
    text = "---\nname: x\n---\n\nBody line."
    result = _strip_frontmatter(text)
    assert result.startswith("Body line.")


# ---------------------------------------------------------------------------
# _overflow_ref_text
# ---------------------------------------------------------------------------


def test_overflow_ref_text_contains_path() -> None:
    """Tier 2: overflow message includes the stored path."""
    ref = {"path": "/media/img1.png", "mime_type": "image/png"}
    text = _overflow_ref_text(ref)
    assert "/media/img1.png" in text


def test_overflow_ref_text_contains_mime_type() -> None:
    """Tier 2: overflow message includes the mime type."""
    ref = {"path": "/media/img1.png", "mime_type": "image/jpeg"}
    text = _overflow_ref_text(ref)
    assert "image/jpeg" in text


def test_overflow_ref_text_fallback_mime_type() -> None:
    """Tier 2: missing mime_type falls back to 'image'."""
    ref = {"path": "/media/img1.png"}
    text = _overflow_ref_text(ref)
    assert "image" in text


# ---------------------------------------------------------------------------
# _is_context_overflow_error
# ---------------------------------------------------------------------------


def test_is_context_overflow_error_context_keyword() -> None:
    """Tier 2: exception message containing 'context' → True."""
    assert _is_context_overflow_error(Exception("context window exceeded")) is True


def test_is_context_overflow_error_token_keyword() -> None:
    """Tier 2: exception message containing 'token' → True."""
    assert _is_context_overflow_error(Exception("too many tokens")) is True


def test_is_context_overflow_error_length_keyword() -> None:
    """Tier 2: exception message containing 'length' → True."""
    assert _is_context_overflow_error(Exception("max length exceeded")) is True


def test_is_context_overflow_error_unrelated_exception() -> None:
    """Tier 2: exception without any overflow keyword → False."""
    assert _is_context_overflow_error(Exception("network connection refused")) is False


def test_is_context_overflow_error_case_insensitive() -> None:
    """Tier 2: keyword match is case-insensitive."""
    assert _is_context_overflow_error(Exception("CONTEXT_LENGTH_EXCEEDED")) is True


# ---------------------------------------------------------------------------
# _is_unsupported_param_error
# ---------------------------------------------------------------------------


def test_is_unsupported_param_error_class_name() -> None:
    """Tier 2: exception class name containing 'UnsupportedParams' → True."""

    class UnsupportedParamsError(Exception):
        pass

    assert _is_unsupported_param_error(UnsupportedParamsError("bad param")) is True


def test_is_unsupported_param_error_encoding_format() -> None:
    """Tier 2: exception message containing 'encoding_format' → True."""
    assert _is_unsupported_param_error(Exception("encoding_format not supported")) is True


def test_is_unsupported_param_error_does_not_support_message() -> None:
    """Tier 2: 'does not support parameter' message → True."""
    assert _is_unsupported_param_error(Exception("model does not support parameter x")) is True


def test_is_unsupported_param_error_unrelated() -> None:
    """Tier 2: unrelated exception → False."""
    assert _is_unsupported_param_error(ValueError("bad value")) is False


# ---------------------------------------------------------------------------
