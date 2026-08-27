"""Tier 1: contract test for classify_router_error.

The router-loop catch in ``Session`` previously surfaced raw
exceptions as ``router failed: <repr>`` — typically a litellm class
name followed by a multi-line JSON blob, truncated mid-sentence by the
ErrorBox renderer. This module classifies the common buckets so the
user sees an actionable prefix and hint.

Pins the public function behaviour against a representative set of
provider exception shapes, including the BudgetExceeded path.
"""
from __future__ import annotations

import pytest

from reyn.runtime.budget.budget import BudgetExceeded
from reyn.runtime.error_format import (
    classify_router_error,
    is_quota_exhausted_error,
    quota_reset_seconds,
)

# ── synthetic exception shapes mimicking provider classes ────────────────────


class RateLimitError(Exception):
    pass


class AnthropicRateLimitError(Exception):
    """Subclass-named variant — substring match on class name still classifies."""


class AuthenticationError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class ServiceUnavailableError(Exception):
    pass


class InternalServerError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class BadRequestError(Exception):
    pass


class WrappedAPIError(Exception):
    """Provider variant that surfaces every failure through a single class
    + a status_code attribute — classifier must fall back to the code."""

    def __init__(self, msg: str, status_code: int):
        super().__init__(msg)
        self.status_code = status_code


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc, expected_label",
    [
        (RateLimitError("429 too many requests"), "rate limit"),
        (AnthropicRateLimitError("anthropic 429"), "rate limit"),
        (AuthenticationError("invalid api key"), "auth error"),
        (APITimeoutError("Request timed out"), "timeout"),
        (ServiceUnavailableError("503"), "provider error"),
        (InternalServerError("500"), "provider error"),
        (APIConnectionError("connect failed"), "connection error"),
        (BadRequestError("context too long"), "bad request"),
    ],
)
def test_class_name_classification(exc: Exception, expected_label: str) -> None:
    """Tier 1: each provider class-name maps to the expected bucket label."""
    out = classify_router_error(exc)
    assert out.startswith(f"router failed: [{expected_label}]"), out
    # Hint must be present (after the bullet) for actionable guidance
    assert " • " in out, f"missing hint separator: {out}"


@pytest.mark.parametrize(
    "code, expected_label",
    [
        (429, "rate limit"),
        (401, "auth error"),
        (403, "auth error"),
        (408, "timeout"),
        (500, "provider error"),
        (502, "provider error"),
        (503, "provider error"),
        (599, "provider error"),
        (400, "bad request"),
    ],
)
def test_status_code_fallback_classification(code: int, expected_label: str) -> None:
    """Tier 1: when class name is generic, status_code drives the bucket."""
    out = classify_router_error(WrappedAPIError("opaque error", code))
    assert out.startswith(f"router failed: [{expected_label}]"), out


def test_budget_exceeded_gets_dedicated_bucket() -> None:
    """Tier 1: BudgetExceeded gets the [budget exceeded] prefix + /budget reset hint."""
    exc = BudgetExceeded("daily_tokens", "daily token cap: 100000/100000 (day: 2026-05-17)")
    out = classify_router_error(exc)
    assert "[budget exceeded]" in out
    assert "daily_tokens" in out
    assert "/budget reset" in out


def test_unknown_exception_falls_back_to_repr() -> None:
    """Tier 1: unmatched exception class returns the original message intact."""
    out = classify_router_error(ValueError("something weird happened"))
    assert out == "router failed: something weird happened"


def test_multiline_message_is_trimmed_to_first_line() -> None:
    """Tier 1: provider exceptions often carry multi-line JSON; only line 1 surfaces."""
    msg = 'RateLimitError\n{"type":"error","message":"..."}\nextra'
    out = classify_router_error(RateLimitError(msg))
    assert "\n" not in out, f"newline leaked into user-facing text: {out!r}"
    assert "[rate limit]" in out


def test_very_long_message_is_truncated() -> None:
    """Tier 1: a 500-char one-liner from a provider gets truncated with an ellipsis."""
    huge = "x" * 500
    out = classify_router_error(RateLimitError(huge))
    assert out.endswith("• wait a moment then retry"), out
    # Truncation marker must be present
    assert "…" in out


# ── #5256: quota exhaustion is a distinct 429 shape, not a plain rate limit ──


class _QuotaExhaustedRateLimitError(Exception):
    """Real shape observed (#5256): a RateLimitError whose litellm ``.body``
    carries the provider's own structured usage-window/plan quota fields."""

    def __init__(self, *, resets_in_seconds: "int | None" = None) -> None:
        super().__init__("The usage limit has been reached")
        self.status_code = 429
        body = {"type": "usage_limit_reached", "message": "The usage limit has been reached"}
        if resets_in_seconds is not None:
            body["resets_in_seconds"] = resets_in_seconds
        self.body = body


def test_is_quota_exhausted_error_reads_the_structured_body_type() -> None:
    """Tier 1: the discriminator is the STRUCTURED ``body.type`` field, never
    ``str(exc)`` (#5257's own precedent — a provider's free-text wording is
    not reyn's contract to match)."""
    assert is_quota_exhausted_error(_QuotaExhaustedRateLimitError()) is True
    # A plain RateLimitError with no such body is NOT quota-exhaustion —
    # non-vacuity: the predicate must not fire on every RateLimitError.
    assert is_quota_exhausted_error(RateLimitError("429 too many requests")) is False
    # A body dict present but with a DIFFERENT type must not match either.
    other = RateLimitError("429")
    other.body = {"type": "some_other_reason"}
    assert is_quota_exhausted_error(other) is False


def test_quota_reset_seconds_reads_resets_in_seconds_not_resets_at() -> None:
    """Tier 1: #5256 — a real occurrence computed a wait time from
    ``resets_at`` (an absolute epoch) that was wrong by ~4 days against the
    account's actual recovery; ``resets_in_seconds`` (a duration, used
    as-is, never derived) is the field this reads."""
    exc = _QuotaExhaustedRateLimitError(resets_in_seconds=12258)
    exc.body["resets_at"] = 1788132890  # present, must be ignored
    assert quota_reset_seconds(exc) == 12258

    absent = _QuotaExhaustedRateLimitError()
    assert quota_reset_seconds(absent) is None


def test_quota_exhaustion_gets_its_own_bucket_with_the_reset_duration() -> None:
    """Tier 1: the [usage limit] bucket is distinct from [rate limit] and
    surfaces the provider's own reported wait — never the provider's own
    wording (#5257), and never a value derived from resets_at."""
    out = classify_router_error(_QuotaExhaustedRateLimitError(resets_in_seconds=12258))
    assert out.startswith("router failed: [usage limit]"), out
    assert "12258" in out


def test_quota_exhaustion_without_a_reset_duration_still_gets_the_bucket() -> None:
    """Tier 1: non-vacuity for the missing-duration branch — the bucket
    still fires and still says something decision-enabling, not a crash
    on a missing field."""
    out = classify_router_error(_QuotaExhaustedRateLimitError())
    assert out.startswith("router failed: [usage limit]"), out


def test_plain_rate_limit_is_unaffected_by_the_new_bucket() -> None:
    """Tier 2: non-vacuity — an ordinary RateLimitError with no quota body
    still lands in the pre-existing [rate limit] bucket, unchanged."""
    out = classify_router_error(RateLimitError("429 too many requests"))
    assert out.startswith("router failed: [rate limit]"), out
