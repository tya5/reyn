"""Tier 2: OS-invariant tests for #1835 — jitter + Retry-After in the LLM self-retry layer.

Pinned invariants:

- ``_backoff_s`` with jitter=true produces sleep times in ``[base/2, base]``
  and NOT all equal to the pure-exponential value (= jitter is actually applied,
  not a no-op). base = min(2.0 * 2**attempt, 16.0).
- ``_backoff_s`` with jitter=false returns the pure-exponential value (2s, 4s,
  8s, 16s) — behaviour is preserved as opt-out.
- ``_extract_retry_after`` parses delta-seconds and HTTP-date forms of the
  ``Retry-After`` header from a real httpx.Response on a real litellm exception.
- ``_extract_retry_after`` returns None when no header is present.
- ``_extract_retry_after`` caps at ``_LLM_RETRY_MAX_BACKOFF_S``.
- ``_llm_call_with_retry`` with respect_retry_after=true uses the parsed
  Retry-After value (not jittered backoff) when the exception carries a header.
- ``_llm_call_with_retry`` with respect_retry_after=false ignores the header
  and uses jittered backoff instead.

Testing policy compliance (testing.ja.md):
- sleep_fn is an injectable parameter on ``_llm_call_with_retry`` — a real
  callable seam (not a mock). The shim records durations; calling convention
  matches asyncio.sleep exactly.
- Exceptions are constructed as real litellm / httpx objects (not mocks).
  A real httpx.Response carries the Retry-After header so _extract_retry_after
  can access it via exc.response.headers — the real chain.
- No MagicMock / AsyncMock / patch. The retry_config ContextVar is set /
  cleared with the real set_retry_config + ContextVar.reset() seam.
- Private state is NOT asserted. The test observes recorded sleep durations
  (the public side-effect of the retry) and return values.
"""
from __future__ import annotations

import asyncio
import datetime
from email.utils import format_datetime
from typing import Any

import httpx
import litellm
import pytest

import reyn.llm.llm as llm_mod
from reyn.config.infra import RetryConfig
from reyn.llm.llm import (
    _LLM_RETRY_MAX_BACKOFF_S,
    _backoff_s,
    _extract_retry_after,
    _llm_call_with_retry,
    _retry_config_var,
    set_retry_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RecordingSleep:
    """Callable shim for sleep_fn= injection.

    Records each requested duration so tests can assert on what durations
    were passed.  Returns immediately so tests don't actually wait.
    Matches the asyncio.sleep(delay) signature (a single positional arg).
    """

    def __init__(self) -> None:
        self.durations: list[float] = []

    async def __call__(self, delay: float) -> None:  # noqa: D105
        self.durations.append(delay)


def _make_exc_with_retry_after(header_value: str, status: int = 503):
    """Real litellm exception carrying a Retry-After header on a real httpx.Response."""
    response = httpx.Response(
        status_code=status,
        headers={"retry-after": header_value},
        text="service unavailable",
    )
    return litellm.exceptions.ServiceUnavailableError(
        message="503 test",
        response=response,
        llm_provider="openai",
        model="openai/gpt-4o-mini",
    )


def _make_exc_no_retry_after(status: int = 503):
    """Real litellm exception with no Retry-After header."""
    response = httpx.Response(
        status_code=status,
        text="service unavailable",
    )
    return litellm.exceptions.ServiceUnavailableError(
        message="503 test",
        response=response,
        llm_provider="openai",
        model="openai/gpt-4o-mini",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_retry_config():
    """Restore the retry_config ContextVar to None after each test."""
    token = _retry_config_var.set(None)
    yield
    _retry_config_var.reset(token)


# ---------------------------------------------------------------------------
# _backoff_s — jitter on/off
# ---------------------------------------------------------------------------

def test_backoff_s_jitter_on_stays_within_bounds():
    """Tier 2: _backoff_s with jitter=true stays within [base/2, base].

    Verifies the equal-jitter bound holds for all 4 exponential steps.
    We run 20 samples per step to get statistical confidence that the
    range constraint is not accidentally met by a single draw.
    """
    set_retry_config(RetryConfig(jitter=True, respect_retry_after=False))
    for attempt in range(4):  # 0→2s, 1→4s, 2→8s, 3→16s
        base = min(2.0 * (2 ** attempt), 16.0)
        lo, hi = base / 2, base
        for _ in range(20):
            s = _backoff_s(attempt)
            assert lo <= s <= hi, (
                f"attempt={attempt}: sleep={s:.4f} outside [{lo:.2f}, {hi:.2f}]"
            )


def test_backoff_s_jitter_on_not_all_identical():
    """Tier 2: _backoff_s with jitter=true produces non-constant values.

    With 50 samples the probability that all land on the same float by
    chance is negligible — if jitter were a no-op, all values would be
    the pure-exponential constant and this assertion would fail.
    """
    set_retry_config(RetryConfig(jitter=True, respect_retry_after=False))
    samples = [_backoff_s(0) for _ in range(50)]
    assert len(set(samples)) > 1, (
        "All 50 samples of _backoff_s(0) were identical — jitter is a no-op"
    )


def test_backoff_s_jitter_off_is_pure_exponential():
    """Tier 2: _backoff_s with jitter=false returns exact exponential values.

    Preserves the pre-#1835 behaviour when the operator opts out of jitter.
    """
    set_retry_config(RetryConfig(jitter=False, respect_retry_after=False))
    assert _backoff_s(0) == 2.0
    assert _backoff_s(1) == 4.0
    assert _backoff_s(2) == 8.0
    assert _backoff_s(3) == 16.0   # capped at max


# ---------------------------------------------------------------------------
# _extract_retry_after — header parsing
# ---------------------------------------------------------------------------

def test_extract_retry_after_delta_seconds():
    """Tier 2: _extract_retry_after parses delta-seconds from exc.response.headers.

    Uses 7 seconds — well below the 16 s cap — so the raw parsed value is
    returned unmodified (cap does not mask the parsing result).
    """
    exc = _make_exc_with_retry_after("7")
    result = _extract_retry_after(exc)
    assert result == pytest.approx(7.0)


def test_extract_retry_after_delta_seconds_capped():
    """Tier 2: _extract_retry_after caps the value at _LLM_RETRY_MAX_BACKOFF_S."""
    exc = _make_exc_with_retry_after("999")
    result = _extract_retry_after(exc)
    assert result == pytest.approx(_LLM_RETRY_MAX_BACKOFF_S)


def test_extract_retry_after_http_date_future():
    """Tier 2: _extract_retry_after parses HTTP-date and computes positive delta."""
    # Use a date far in the future (2099) so the delta is always positive.
    future = datetime.datetime(2099, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    header_val = format_datetime(future, usegmt=True)
    exc = _make_exc_with_retry_after(header_val)
    result = _extract_retry_after(exc)
    # The delta is a large positive number; cap is 16.0.
    assert result == pytest.approx(_LLM_RETRY_MAX_BACKOFF_S)


def test_extract_retry_after_http_date_past_clamps_zero():
    """Tier 2: _extract_retry_after clamps negative HTTP-date deltas to 0."""
    past = datetime.datetime(2000, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    header_val = format_datetime(past, usegmt=True)
    exc = _make_exc_with_retry_after(header_val)
    result = _extract_retry_after(exc)
    assert result == pytest.approx(0.0)


def test_extract_retry_after_no_header_returns_none():
    """Tier 2: _extract_retry_after returns None when no Retry-After header."""
    exc = _make_exc_no_retry_after()
    result = _extract_retry_after(exc)
    assert result is None


def test_extract_retry_after_unparseable_returns_none():
    """Tier 2: _extract_retry_after ignores unparseable header values."""
    exc = _make_exc_with_retry_after("not-a-number-or-date")
    result = _extract_retry_after(exc)
    assert result is None


# ---------------------------------------------------------------------------
# _llm_call_with_retry — integration of jitter + Retry-After via sleep_fn seam
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_uses_retry_after_when_header_present():
    """Tier 2: retry loop honours Retry-After header when respect_retry_after=true.

    Constructs a real ServiceUnavailableError with Retry-After: 7 on a real
    httpx.Response. The first call raises it; the second succeeds (simulates a
    transient 503). The recorded sleep must be ~7 (within float tolerance) and
    NOT the jittered exponential (which for attempt=0 would be in [1.0, 2.0]).
    """
    set_retry_config(RetryConfig(jitter=True, respect_retry_after=True))
    exc = _make_exc_with_retry_after("7")

    # A real mock-free callable sequence: first call raises, second returns.
    class _FakeResponse:
        choices = [object()]  # non-empty → not EmptyLLMResponseError

    calls = [0]

    async def _coro_fn() -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise exc
        return _FakeResponse()

    recorder = _RecordingSleep()
    result = await _llm_call_with_retry(
        _coro_fn, model="test-model", event_log=None, sleep_fn=recorder
    )
    assert result is not None
    # One retry occurred (first call failed, second succeeded) → one sleep was recorded.
    assert recorder.durations, "expected at least one sleep from the retry"
    # The sleep value must come from the Retry-After header (7 s), not jittered backoff.
    assert recorder.durations[-1] == pytest.approx(7.0, abs=0.01)


@pytest.mark.asyncio
async def test_retry_uses_jittered_backoff_when_no_header():
    """Tier 2: retry loop uses jittered backoff when no Retry-After header.

    The first call raises a real ServiceUnavailableError with no header.
    The recorded sleep must fall in [1.0, 2.0] (= [base/2, base] for attempt=0,
    base=2.0) — NOT the exact pure-exponential 2.0.
    """
    set_retry_config(RetryConfig(jitter=True, respect_retry_after=True))
    exc = _make_exc_no_retry_after()

    class _FakeResponse:
        choices = [object()]

    calls = [0]

    async def _coro_fn() -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise exc
        return _FakeResponse()

    recorder = _RecordingSleep()
    await _llm_call_with_retry(
        _coro_fn, model="test-model", event_log=None, sleep_fn=recorder
    )
    assert recorder.durations, "expected at least one sleep from the retry"
    s = recorder.durations[-1]
    assert 1.0 <= s <= 2.0, (
        f"Expected jittered sleep in [1.0, 2.0] for attempt=0, got {s:.4f}"
    )


@pytest.mark.asyncio
async def test_retry_ignores_retry_after_when_disabled():
    """Tier 2: retry loop ignores Retry-After header when respect_retry_after=false.

    The exception carries Retry-After: 99. With respect_retry_after=false and
    jitter=false, the sleep must be the pure-exponential 2.0 (attempt=0),
    NOT 99.
    """
    set_retry_config(RetryConfig(jitter=False, respect_retry_after=False))
    exc = _make_exc_with_retry_after("99")

    class _FakeResponse:
        choices = [object()]

    calls = [0]

    async def _coro_fn() -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise exc
        return _FakeResponse()

    recorder = _RecordingSleep()
    await _llm_call_with_retry(
        _coro_fn, model="test-model", event_log=None, sleep_fn=recorder
    )
    assert recorder.durations, "expected at least one sleep from the retry"
    # Must be the pure-exponential 2.0 (attempt=0), NOT the 99 s from the header.
    assert recorder.durations[-1] == pytest.approx(2.0)  # pure-exponential attempt=0
