"""Tier 2: pure helpers in runtime/router_loop.py, plus the memory-layer
``strip_frontmatter`` this module has always covered (it moved to
runtime/services/memory_service.py with the read_body operation that calls it).

``_overflow_ref_text(ref)``           — format image-overflow reference message
``is_context_overflow_error(exc)``    — context-length-overflow classification
                                         (#3783 stage 1: moved from
                                         router_loop.py's own
                                         ``_is_context_overflow_error`` — one
                                         of 5 divergent copies — to
                                         ``reyn.services.compaction.engine``,
                                         the single shared owner)
``_is_unsupported_param_error(exc)``  — class-name/keyword unsupported param errors
"""
from __future__ import annotations

import sys

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.runtime.router_loop import (
    _is_unsupported_param_error,
    _overflow_ref_text,
)

# #3607: strip_frontmatter is a memory-layer rule and moved to MemoryService's
# module with the operations that use it; it is public there (read_body is its
# only caller and lives beside it).
from reyn.runtime.services.memory_service import strip_frontmatter as _strip_frontmatter
from reyn.services.compaction.engine import is_context_overflow_error

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
# is_context_overflow_error (#3783 stage 1: moved from router_loop.py)
# ---------------------------------------------------------------------------


def test_is_context_overflow_error_context_keyword() -> None:
    """Tier 2: exception message containing 'context' → True."""
    assert is_context_overflow_error(Exception("context window exceeded")) is True


def test_is_context_overflow_error_token_keyword() -> None:
    """Tier 2: exception message containing 'token' → True."""
    assert is_context_overflow_error(Exception("too many tokens")) is True


def test_is_context_overflow_error_length_keyword() -> None:
    """Tier 2: exception message containing 'length' → True."""
    assert is_context_overflow_error(Exception("max length exceeded")) is True


def test_is_context_overflow_error_too_long_keyword() -> None:
    """Tier 2: #3783 — exception message containing 'too long' → True.

    This keyword (and 'too large' below) was MISSING from
    compaction/engine.py's own pre-#3783 copy of this predicate — one of
    the 5 divergent copies stage 1 unified. Pinned here because it is the
    one keyword whose coverage actually changed for a real call site.
    """
    assert is_context_overflow_error(Exception("the prompt is too long")) is True


def test_is_context_overflow_error_too_large_keyword() -> None:
    """Tier 2: #3783 — exception message containing 'too large' → True (see
    the 'too long' test above for why this pair is pinned separately)."""
    assert is_context_overflow_error(Exception("input is too large")) is True


def test_is_context_overflow_error_unrelated_exception() -> None:
    """Tier 2: exception without any overflow keyword → False."""
    assert is_context_overflow_error(Exception("network connection refused")) is False


def test_is_context_overflow_error_case_insensitive() -> None:
    """Tier 2: keyword match is case-insensitive."""
    assert is_context_overflow_error(Exception("CONTEXT_LENGTH_EXCEEDED")) is True


def test_is_context_overflow_error_recognises_the_real_litellm_type() -> None:
    """Tier 2: #3783 — a real ``litellm.ContextWindowExceededError`` (not a
    plain ``Exception`` standing in for one) is recognised.

    A real litellm exception happens to also carry its own class name inside
    ``str(exc)`` (verified below), so this alone does not tell type-first
    apart from substring-only. The two tests after this one construct
    exceptions where the two signals genuinely DISAGREE, which is where
    "type checked first" is actually observable — see lead-coder's review:
    an example where both paths agree is not a witness for the order."""
    import litellm

    exc = litellm.ContextWindowExceededError(
        message="the model declined this request",
        model="gpt-4o", llm_provider="openai",
    )
    assert "context" in str(exc).lower(), (
        "test premise: litellm's own class name leaks a keyword into str(exc) "
        "here, which is exactly why this test cannot isolate the type check"
    )
    assert is_context_overflow_error(exc) is True


def test_is_context_overflow_error_type_alone_recovers_when_message_has_no_keyword() -> None:
    """Tier 2: #3783 — the type check is load-bearing on its own, not merely
    a faster path to the same answer the substring fallback would give.

    Constructs a genuine subclass of ``litellm.ContextWindowExceededError``
    (a real instance, not a mock) whose ``__str__`` is overridden to contain
    NONE of ``_CONTEXT_OVERFLOW_KEYWORDS`` — the case a real litellm proxy
    could produce if it ever normalised the message text while preserving
    the typed exception. Falsification (performed during review, not
    re-run automatically here — it requires temporarily removing the
    ``isinstance`` check from the production function): with the type
    check removed, this test goes RED (``is_context_overflow_error``
    returns ``False``) — confirming the type check is what this test
    actually exercises, not a redundant fast path over the fallback."""
    import litellm

    class _RewordedOverflow(litellm.ContextWindowExceededError):
        def __str__(self) -> str:
            return "the request could not be completed"

    exc = _RewordedOverflow(message="irrelevant", model="m", llm_provider="p")
    assert not any(
        kw in str(exc).lower()
        for kw in ("context", "token", "length", "limit", "too long", "too large")
    ), "test premise: the overridden __str__ must carry no overflow keyword"
    assert isinstance(exc, litellm.ContextWindowExceededError)  # sanity: real subclass
    assert is_context_overflow_error(exc) is True


def test_is_context_overflow_error_substring_still_catches_a_flattened_type() -> None:
    """Tier 2: #3783 — the substring fallback is load-bearing for the case
    the type check CANNOT catch: a litellm proxy that flattens a provider's
    typed overflow error down to a bare ``BadRequestError`` (not the
    ``ContextWindowExceededError`` subclass), keeping only the message
    text. A real (not mocked) ``litellm.BadRequestError`` instance, which
    is NOT an instance of ``ContextWindowExceededError``.

    Falsification (performed during review, not re-run automatically
    here): with the substring fallback removed (type check only), this
    test goes RED — confirming the fallback is load-bearing for this
    case, not dead code the type check already covers."""
    import litellm

    exc = litellm.BadRequestError(
        message="the context length exceeds the model's limit",
        model="m", llm_provider="p",
    )
    assert not isinstance(exc, litellm.ContextWindowExceededError), (
        "test premise: a flattened BadRequestError, not the typed subclass"
    )
    assert is_context_overflow_error(exc) is True


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
