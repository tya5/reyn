"""Tier 1: contract test for classify_router_error.

The router-loop catch in ``ChatSession`` previously surfaced raw
exceptions as ``router failed: <repr>`` — typically a litellm class
name followed by a multi-line JSON blob, truncated mid-sentence by the
ErrorBox renderer. This module classifies the common buckets so the
user sees an actionable prefix and hint.

Pins the public function behaviour against a representative set of
provider exception shapes, including the BudgetExceeded path.
"""
from __future__ import annotations

import pytest

from reyn.chat.error_format import classify_router_error
from reyn.runtime.budget.budget import BudgetExceeded

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
