"""Tier 2: #5543 / #5531 §10 — ``classify_llm_failure`` classifies an
exception into exactly one of FATAL / RETRYABLE / OVERFLOW.

Owner's own reason for requiring this (#3783, verbatim): "An
``AttributeError`` in our own code must not become 'quietly shrink, then
``UnrecoveredError``'" — removing the shrink ladder's iteration cap
without this classification existing first would let a genuine reyn code
bug get silently shrunk through the whole floor before failing with the
wrong diagnosis. This file proves the classification itself, independent
of the (separate, not-yet-landed) wiring of that classification into
``retry_loop``'s own control flow.

Policy (testing.md): real exception instances/subclasses (a minimal
stand-in reproducing a provider's own attribute shape, same pattern
``test_4381_stage1_overflow_classification.py`` already established) —
no mocks.
"""
from __future__ import annotations

from reyn.services.compaction.engine import LLMFailureClass, classify_llm_failure


class _FakeStatusError(Exception):
    """A minimal stand-in for a provider exception's own status_code
    attribute shape — not a litellm/openai subclass, so this exercises
    the ATTRIBUTE/status_code check independent of whichever litellm
    exception hierarchy happens to be installed (same pattern as
    test_4381_stage1_overflow_classification.py's own fixture)."""

    def __init__(self, message: str, *, status_code: "int | None" = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def test_typeerror_attributeerror_keyerror_are_fatal() -> None:
    """Tier 2: the closed reyn-own-code-bug allowlist — never shrunk,
    never treated as an overflow, regardless of message wording."""
    for exc in (
        TypeError("unsupported operand"),
        AttributeError("'NoneType' object has no attribute 'x'"),
        KeyError("missing_field"),
    ):
        assert classify_llm_failure(exc) is LLMFailureClass.FATAL


def test_auth_error_by_class_name_is_fatal() -> None:
    """Tier 2: a credential failure — class-name signal (mirrors
    error_format.py's own auth bucket detection)."""
    class AuthenticationError(_FakeStatusError):
        pass

    assert classify_llm_failure(AuthenticationError("bad key")) is LLMFailureClass.FATAL


def test_auth_error_by_status_code_is_fatal() -> None:
    """Tier 2: 401/403 via status_code alone (a provider that wraps
    everything in one generic exception class) is ALSO fatal — the
    status-code signal, not just the class name, decides this."""
    for code in (401, 403):
        exc = _FakeStatusError("permission denied", status_code=code)
        assert classify_llm_failure(exc) is LLMFailureClass.FATAL


def test_quota_exhaustion_is_retryable_not_fatal_not_overflow() -> None:
    """Tier 2: #5256's own quota-exhaustion shape (a RateLimitError whose
    structured body names usage_limit_reached) classifies RETRYABLE — the
    SAME class an ordinary rate limit gets, distinct from both FATAL and
    OVERFLOW despite superficially being a 429."""
    class RateLimitError(_FakeStatusError):
        pass

    exc = RateLimitError("usage limit reached", status_code=429)
    exc.body = {"type": "usage_limit_reached", "resets_in_seconds": 3600}
    assert classify_llm_failure(exc) is LLMFailureClass.RETRYABLE


def test_ordinary_rate_limit_429_is_retryable() -> None:
    """Tier 2: a per-request rate limit (no quota body) — same bucket as
    quota exhaustion (#5543's own grouping), distinguishable from FATAL
    and OVERFLOW."""
    class RateLimitError(_FakeStatusError):
        pass

    assert classify_llm_failure(RateLimitError("slow down", status_code=429)) \
        is LLMFailureClass.RETRYABLE


def test_5xx_and_infra_errors_are_retryable() -> None:
    """Tier 2: the SAME infra shapes llm.py's own ``_is_retryable_exc``
    already retries — timeout / connection / 5xx."""
    class Timeout(_FakeStatusError):
        pass

    class APIConnectionError(_FakeStatusError):
        pass

    class ServiceUnavailableError(_FakeStatusError):
        pass

    assert classify_llm_failure(Timeout("timed out")) is LLMFailureClass.RETRYABLE
    assert classify_llm_failure(APIConnectionError("dns failure")) is LLMFailureClass.RETRYABLE
    assert classify_llm_failure(ServiceUnavailableError("x", status_code=503)) \
        is LLMFailureClass.RETRYABLE
    assert classify_llm_failure(_FakeStatusError("x", status_code=500)) \
        is LLMFailureClass.RETRYABLE


def test_413_and_context_length_are_overflow() -> None:
    """Tier 2: a genuine context/body-size overflow — the ONLY class that
    enters the shrink ladder."""
    assert classify_llm_failure(_FakeStatusError("x", status_code=413)) \
        is LLMFailureClass.OVERFLOW
    assert classify_llm_failure(Exception("maximum context length exceeded")) \
        is LLMFailureClass.OVERFLOW


def test_unrecognised_exception_falls_through_to_overflow() -> None:
    """Tier 2: an exception matching none of the FATAL/RETRYABLE signals
    falls through to OVERFLOW — the pre-#5543 implicit default this ladder
    already had (its except clause only ever caught already overflow-
    wrapped exceptions), not a new widening of what this function
    accepts."""
    assert classify_llm_failure(Exception("something the provider invented")) \
        is LLMFailureClass.OVERFLOW


def test_fatal_precedence_over_overflow_wording() -> None:
    """Tier 2: precedence — a FATAL type wins even if its own message
    happens to contain overflow-shaped wording (a defensive precedence
    check, not merely today's observed shape: the check ORDER, not just
    the individual predicates, is the thing under test here)."""
    exc = AttributeError("context length exceeded somehow")
    assert classify_llm_failure(exc) is LLMFailureClass.FATAL


def test_retryable_precedence_over_overflow_wording() -> None:
    """Tier 2: RETRYABLE is checked before the OVERFLOW keyword fallback
    — a 5xx whose message happens to mention "limit" (one of the overflow
    keywords) must not misclassify as OVERFLOW."""
    exc = _FakeStatusError("rate limit exceeded, too large a burst", status_code=429)
    assert classify_llm_failure(exc) is LLMFailureClass.RETRYABLE
