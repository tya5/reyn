"""Tier 2: OS invariant — LLM call infrastructure retry + event observability.

Guards the retry wrapper around the LiteLLM call boundary introduced in
FP-0008 PR-Q v8:

1. test_no_retry_on_success
   Successful call on first attempt → no retry, no retry events emitted.

2. test_retry_and_succeed
   Timeout on attempt 1 → retry fires, succeeds on attempt 2.
   llm_call_retry event emitted with correct fields.

3. test_all_retries_exhausted
   Timeout on all 3 attempts → llm_call_retry_exhausted event emitted,
   exception propagates.

4. test_4xx_no_retry
   BadRequestError (4xx semantic) → immediate failure, no retry, no events.

5. test_backoff_shape
   Backoff values follow the exponential curve: _backoff_s(0)=2s,
   _backoff_s(1)=4s, _backoff_s(2)=8s — capped at 16s.

6. test_httpx_errors_retried
   Raw httpx.ConnectError and httpx.ReadTimeout are retried (= transport-level
   errors LiteLLM may not wrap).

9. test_empty_choices_retried_then_succeed   (#187 B1)
   A 200 response with choices=[] on attempt 1 → retried as a transient
   condition, succeeds on attempt 2. No IndexError, no crash.

10. test_empty_choices_exhausted_raises_named_error   (#187 B1)
   choices=[] on every attempt → raises the named EmptyLLMResponseError
   (NOT a cryptic IndexError from response.choices[0]); exhausted event
   emitted. Pins the real proxy failure-mode shape.

No real network calls — tests use a counter-based async callable stub that
fails K times then succeeds (real instance, no unittest.mock).
asyncio.sleep is monkeypatched to a no-op so tests run at full speed.
"""
from __future__ import annotations

import httpx
import litellm
import pytest

from reyn.events.events import EventLog
from reyn.llm.llm import (
    _LLM_RETRY_BASE_S,
    _LLM_RETRY_MAX_ATTEMPTS,
    _LLM_RETRY_MAX_BACKOFF_S,
    EmptyLLMResponseError,
    _backoff_s,
    _empty_response_diag,
    _env_num,
    _is_retryable_exc,
    _llm_call_with_retry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event_log() -> EventLog:
    """Real EventLog with no persistent subscribers (in-memory only)."""
    return EventLog()


class _FailThenSucceedCallable:
    """Zero-arg async callable: raises ``exc`` for the first ``fail_count``
    calls, then returns ``success_response``.

    Real instance — no mock machinery.
    """

    def __init__(self, fail_count: int, exc: BaseException, success_response: object) -> None:
        self._fail_count = fail_count
        self._exc = exc
        self._success = success_response
        self.call_count: int = 0

    async def __call__(self) -> object:
        self.call_count += 1
        if self.call_count <= self._fail_count:
            raise self._exc
        return self._success


class _AlwaysFailCallable:
    """Zero-arg async callable that always raises ``exc``."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.call_count: int = 0

    async def __call__(self) -> object:
        self.call_count += 1
        raise self._exc


def _fake_response(content: str = '{"ok": true}') -> object:
    """Minimal litellm-response-shaped object for testing."""
    class _Choice:
        class _Msg:
            def __init__(self, c: str) -> None:
                self.content = c
                self.tool_calls = None
        message = _Msg(content)
        finish_reason = "stop"

    class _Response:
        choices = [_Choice()]
        usage = None

    return _Response()


def _fake_empty_response() -> object:
    """A 200-shaped response with an empty ``choices`` list.

    This is the real failure shape the LiteLLM proxy intermittently returns
    for gemini-2.5-flash-lite (#187 B1) — a successful response object whose
    ``choices`` is empty, so ``response.choices[0]`` would IndexError.
    """
    class _Response:
        choices: list = []
        usage = None

    return _Response()


class _ReturnEmptyThenValidCallable:
    """Zero-arg async callable: returns an empty-choices response for the
    first ``empty_count`` calls, then a valid response.

    Models the transient 200+empty-choices condition (a RETURNED value, not a
    raised exception). Real instance — no mock machinery.
    """

    def __init__(self, empty_count: int, valid_response: object) -> None:
        self._empty_count = empty_count
        self._valid = valid_response
        self.call_count: int = 0

    async def __call__(self) -> object:
        self.call_count += 1
        if self.call_count <= self._empty_count:
            return _fake_empty_response()
        return self._valid


class _AlwaysEmptyCallable:
    """Zero-arg async callable that always returns an empty-choices response."""

    def __init__(self) -> None:
        self.call_count: int = 0

    async def __call__(self) -> object:
        self.call_count += 1
        return _fake_empty_response()


# ---------------------------------------------------------------------------
# Test 1: no retry on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_on_success(monkeypatch):
    """Tier 2: retry wrapper — success on first attempt emits no retry events.

    Invariant: when the coro_fn succeeds immediately, _llm_call_with_retry
    must not emit llm_call_retry or llm_call_retry_exhausted.
    """
    import reyn.llm.llm as llm_mod
    monkeypatch.setattr(llm_mod.asyncio, "sleep", lambda _: _no_op_coro())

    log = _make_event_log()
    resp = _fake_response()

    async def _ok():
        return resp

    result = await _llm_call_with_retry(_ok, "model-x", log)
    assert result is resp

    types = [e.type for e in log.all()]
    assert "llm_call_retry" not in types
    assert "llm_call_retry_exhausted" not in types


async def _no_op_coro():
    """Dummy coroutine for asyncio.sleep monkey-patch."""


# ---------------------------------------------------------------------------
# Test 2: retry on timeout, succeed on attempt 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_and_succeed(monkeypatch):
    """Tier 2: retry wrapper — timeout on attempt 1, success on attempt 2.

    Invariant: one llm_call_retry event emitted, no exhausted event, correct
    error_kind and attempt_n fields.
    """
    import reyn.llm.llm as llm_mod
    slept: list[float] = []
    async def _fake_sleep(s: float) -> None:
        slept.append(s)
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep)

    log = _make_event_log()
    resp = _fake_response()
    stub = _FailThenSucceedCallable(
        fail_count=1,
        exc=litellm.exceptions.Timeout("timed out", model="m", llm_provider="p"),
        success_response=resp,
    )

    result = await _llm_call_with_retry(stub, "test-model", log)
    assert result is resp
    assert stub.call_count == 2

    # Exactly one retry event (present) and no exhausted event (absent)
    retry_events = [e for e in log.all() if e.type == "llm_call_retry"]
    assert retry_events, "at least one llm_call_retry event must be emitted after a timeout"
    assert not any(e.type == "llm_call_retry_exhausted" for e in log.all()), (
        "llm_call_retry_exhausted must NOT be emitted when retry succeeds"
    )

    ev = retry_events[0]
    assert ev.data["model"] == "test-model"
    assert ev.data["error_kind"] == "Timeout"
    assert ev.data["attempt_n"] == 1
    assert ev.data["backoff_s"] == _backoff_s(0)

    # Sleep called once with the correct backoff
    assert slept == [_backoff_s(0)]


# ---------------------------------------------------------------------------
# Test 3: all retries exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_retries_exhausted(monkeypatch):
    """Tier 2: retry wrapper — all 3 attempts fail → exhausted event + exception.

    Invariant: llm_call_retry_exhausted emitted on terminal failure; the
    original exception propagates; call_count == _LLM_RETRY_MAX_ATTEMPTS.
    """
    import reyn.llm.llm as llm_mod
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep_noop)

    log = _make_event_log()
    exc = litellm.exceptions.ServiceUnavailableError("503", response=None, llm_provider="test", model="m")
    stub = _AlwaysFailCallable(exc)

    with pytest.raises(litellm.exceptions.ServiceUnavailableError):
        await _llm_call_with_retry(stub, "model-503", log)

    assert stub.call_count == _LLM_RETRY_MAX_ATTEMPTS

    exhausted = [e for e in log.all() if e.type == "llm_call_retry_exhausted"]
    assert exhausted, "llm_call_retry_exhausted must be emitted when all retries fail"
    assert exhausted[0].data["model"] == "model-503"
    assert exhausted[0].data["error_kind"] == "ServiceUnavailableError"


async def _fake_sleep_noop(_: float) -> None:
    pass


# ---------------------------------------------------------------------------
# Test 4: 4xx error — no retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_no_retry(monkeypatch):
    """Tier 2: retry wrapper — BadRequestError (4xx) propagates immediately, no retry.

    Invariant: semantic / validation errors must not be retried (retrying
    won't fix a bad request shape).
    """
    import reyn.llm.llm as llm_mod
    sleep_calls: list[float] = []
    async def _record_sleep(s: float) -> None:
        sleep_calls.append(s)
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _record_sleep)

    log = _make_event_log()
    exc = litellm.exceptions.BadRequestError(
        "invalid request", response=None, llm_provider="test", model="m"
    )
    stub = _AlwaysFailCallable(exc)

    with pytest.raises(litellm.exceptions.BadRequestError):
        await _llm_call_with_retry(stub, "model-bad", log)

    # Only attempted once — no retry
    assert stub.call_count == 1
    assert sleep_calls == []

    types = [e.type for e in log.all()]
    assert "llm_call_retry" not in types
    assert "llm_call_retry_exhausted" not in types


# ---------------------------------------------------------------------------
# Test 5: backoff shape
# ---------------------------------------------------------------------------


def test_backoff_shape():
    """Tier 2: _backoff_s — exponential curve capped at max.

    Verifies the mathematical shape without relying on exact constants so a
    future config change doesn't silently break the invariant.
    """
    # Monotonically increasing up to the cap
    b0 = _backoff_s(0)
    b1 = _backoff_s(1)
    b2 = _backoff_s(2)
    b3 = _backoff_s(3)

    assert b0 > 0, "first backoff must be positive"
    assert b1 > b0, "second backoff must be larger than first"
    assert b2 > b1, "third backoff must be larger than second"

    # Each step doubles up to the cap
    assert b1 == min(b0 * 2, _LLM_RETRY_MAX_BACKOFF_S)
    assert b2 == min(b0 * 4, _LLM_RETRY_MAX_BACKOFF_S)

    # Cap is respected
    assert b3 <= _LLM_RETRY_MAX_BACKOFF_S

    # Concrete values (documentation + regression guard against constant changes)
    assert b0 == _LLM_RETRY_BASE_S
    assert b1 == _LLM_RETRY_BASE_S * 2
    assert b2 == _LLM_RETRY_BASE_S * 4


# ---------------------------------------------------------------------------
# Test 6: httpx transport errors retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_httpx_errors_retried(monkeypatch):
    """Tier 2: retry wrapper — httpx.ConnectError and httpx.ReadTimeout are retried.

    LiteLLM may not wrap transport-level errors that occur before the HTTP
    response is received. The retry wrapper must catch them directly.
    """
    import reyn.llm.llm as llm_mod
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep_noop)

    resp = _fake_response()

    for exc_cls in (httpx.ConnectError, httpx.ReadTimeout):
        log = _make_event_log()
        try:
            raw_exc = exc_cls("connection failed")
        except TypeError:
            # Some httpx exception constructors require a request object
            raw_exc = exc_cls.__new__(exc_cls)

        stub = _FailThenSucceedCallable(
            fail_count=1,
            exc=raw_exc,
            success_response=resp,
        )

        result = await _llm_call_with_retry(stub, "model-net", log)
        assert result is resp, f"{exc_cls.__name__} should be retried"
        assert stub.call_count == 2

        retry_events = [e for e in log.all() if e.type == "llm_call_retry"]
        assert retry_events, f"{exc_cls.__name__}: expected at least one llm_call_retry event"


# ---------------------------------------------------------------------------
# Test 7: _is_retryable_exc classification
# ---------------------------------------------------------------------------


def test_is_retryable_exc_classification():
    """Tier 2: _is_retryable_exc — correct classification of retryable vs non-retryable.

    Checks the classification function directly against concrete exception
    instances without exercising the full retry loop.
    """
    # Retryable
    assert _is_retryable_exc(litellm.exceptions.Timeout("t", model="m", llm_provider="p"))
    assert _is_retryable_exc(litellm.exceptions.APIConnectionError("c", llm_provider="p", model="m"))
    assert _is_retryable_exc(
        litellm.exceptions.InternalServerError("500", response=None, llm_provider="p", model="m")
    )
    assert _is_retryable_exc(
        litellm.exceptions.ServiceUnavailableError("503", response=None, llm_provider="p", model="m")
    )
    assert _is_retryable_exc(
        litellm.exceptions.BadGatewayError("502", response=None, llm_provider="p", model="m")
    )

    # Non-retryable
    assert not _is_retryable_exc(
        litellm.exceptions.BadRequestError("400", response=None, llm_provider="p", model="m")
    )
    assert not _is_retryable_exc(
        litellm.exceptions.AuthenticationError("401", response=None, llm_provider="p", model="m")
    )
    assert not _is_retryable_exc(
        litellm.exceptions.RateLimitError("429", response=None, llm_provider="p", model="m")
    )
    assert not _is_retryable_exc(ValueError("unexpected"))


# ---------------------------------------------------------------------------
# Test 8: event_log=None suppresses events (no crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_log_none_no_crash(monkeypatch):
    """Tier 2: retry wrapper — event_log=None suppresses observability events without crashing.

    Callers that don't pass an EventLog must still benefit from retry behavior.
    """
    import reyn.llm.llm as llm_mod
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep_noop)

    resp = _fake_response()
    stub = _FailThenSucceedCallable(
        fail_count=1,
        exc=litellm.exceptions.Timeout("timed out", model="m", llm_provider="p"),
        success_response=resp,
    )

    # No event_log — must not raise
    result = await _llm_call_with_retry(stub, "model-y", None)
    assert result is resp
    assert stub.call_count == 2


# ---------------------------------------------------------------------------
# Test 9: empty choices (200 + choices=[]) retried, succeeds on retry  (#187 B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_choices_retried_then_succeed(monkeypatch):
    """Tier 2: retry wrapper — a 200 + empty-choices response is retried (#187 B1).

    The LiteLLM proxy intermittently returns a successful response object with
    choices=[] for gemini-2.5-flash-lite. Downstream response.choices[0] would
    IndexError and silently kill the router loop mid-task. Invariant: empty
    choices is treated as a transient condition and retried; the next non-empty
    response is returned, with no IndexError.
    """
    import reyn.llm.llm as llm_mod
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep_noop)

    log = _make_event_log()
    valid = _fake_response()
    stub = _ReturnEmptyThenValidCallable(empty_count=1, valid_response=valid)

    result = await _llm_call_with_retry(stub, "model-empty", log)
    assert result is valid
    assert stub.call_count == 2

    retry_events = [e for e in log.all() if e.type == "llm_call_retry"]
    assert retry_events, "empty choices must emit a llm_call_retry event"
    assert retry_events[0].data["error_kind"] == "EmptyLLMResponseError"
    assert not any(e.type == "llm_call_retry_exhausted" for e in log.all())


# ---------------------------------------------------------------------------
# Test 10: empty choices exhausted → named error, NOT IndexError  (#187 B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_choices_exhausted_raises_named_error(monkeypatch):
    """Tier 2: retry wrapper — persistent empty choices raises a named error (#187 B1).

    Invariant: when every attempt returns choices=[], the wrapper raises the
    explicit EmptyLLMResponseError after exhausting retries — NOT a cryptic
    IndexError from response.choices[0] that the swallow handler would classify
    opaquely. The exhausted event is emitted with the named error kind.
    """
    import reyn.llm.llm as llm_mod
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep_noop)

    log = _make_event_log()
    stub = _AlwaysEmptyCallable()

    with pytest.raises(EmptyLLMResponseError):
        await _llm_call_with_retry(stub, "model-empty-always", log)

    assert stub.call_count == _LLM_RETRY_MAX_ATTEMPTS

    exhausted = [e for e in log.all() if e.type == "llm_call_retry_exhausted"]
    assert exhausted, "persistent empty choices must emit llm_call_retry_exhausted"
    assert exhausted[0].data["error_kind"] == "EmptyLLMResponseError"


# ---------------------------------------------------------------------------
# Test 11-13: empty-choices flake OBSERVABILITY — capture the provider response
# shape (finish_reason / prompt_feedback / usage) so a recurrence is diagnosable
# ---------------------------------------------------------------------------


def test_empty_response_diag_includes_vendor_fields():
    """Tier 2: flake observability — _empty_response_diag dumps the provider response
    shape (the block reason / prompt_feedback / usage live in vendor-specific fields),
    so an empty-choices recurrence is diagnosable, not a bare "empty choices". Real
    Fake with model_dump(), no mock."""
    class _RichEmpty:
        choices: list = []

        def model_dump(self):
            return {
                "choices": [],
                "model": "gemini-2.5-flash-lite",
                "prompt_feedback": {"block_reason": "SAFETY"},
                "usage": {"prompt_tokens": 12, "completion_tokens": 0},
            }

    diag = _empty_response_diag(_RichEmpty())
    assert "SAFETY" in diag          # the actual block reason surfaces
    assert "prompt_feedback" in diag
    assert "usage" in diag


def test_empty_response_diag_best_effort_when_no_model_dump():
    """Tier 2: flake observability — diag is best-effort: a response object without a
    usable model_dump() degrades to repr (returns a non-empty str, never raises) so a
    diag failure can NEVER mask the empty-choices error itself."""
    class _Bare:
        choices: list = []

    diag = _empty_response_diag(_Bare())
    assert isinstance(diag, str) and diag  # non-empty, no exception


@pytest.mark.asyncio
async def test_empty_choices_error_message_carries_provider_diag(monkeypatch):
    """Tier 2: flake observability — the raised EmptyLLMResponseError message carries
    the provider response shape (the WHY), not just the model name, so the exhaustion
    path is diagnosable. Real Fake (model_dump), sleep no-op'd for speed."""
    import reyn.llm.llm as llm_mod
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep_noop)

    class _RichEmptyCallable:
        async def __call__(self):
            class _R:
                choices: list = []

                def model_dump(self):
                    return {"choices": [], "prompt_feedback": {"block_reason": "RECITATION"}}

            return _R()

    with pytest.raises(EmptyLLMResponseError, match="RECITATION"):
        await _llm_call_with_retry(_RichEmptyCallable(), "model-x", _make_event_log())


# ---------------------------------------------------------------------------
# Test 14: retry tuning-knob env-override (REYN_LLM_RETRY_MAX_ATTEMPTS / _BASE_S)
# ---------------------------------------------------------------------------


def test_env_num_retry_knob(monkeypatch):
    """Tier 2: OS invariant — _env_num reads an operator retry tuning knob from the
    environment (clamped, safe fallback), the flaky-provider robustness lever that
    lets a measurement/operator bump retries+backoff without a code change. Default
    preserves today's behaviour. Real env (monkeypatch), no mock."""
    monkeypatch.delenv("REYN_TEST_KNOB", raising=False)
    assert _env_num("REYN_TEST_KNOB", 3, 1, 10, int) == 3          # unset → default
    monkeypatch.setenv("REYN_TEST_KNOB", "5")
    assert _env_num("REYN_TEST_KNOB", 3, 1, 10, int) == 5          # valid override
    monkeypatch.setenv("REYN_TEST_KNOB", "99")
    assert _env_num("REYN_TEST_KNOB", 3, 1, 10, int) == 10         # clamp to hi
    monkeypatch.setenv("REYN_TEST_KNOB", "0")
    assert _env_num("REYN_TEST_KNOB", 3, 1, 10, int) == 1          # clamp to lo
    monkeypatch.setenv("REYN_TEST_KNOB", "not-a-number")
    assert _env_num("REYN_TEST_KNOB", 3, 1, 10, int) == 3          # invalid → default
    monkeypatch.setenv("REYN_TEST_KNOB", "4.5")
    assert _env_num("REYN_TEST_KNOB", 2.0, 0.1, 30.0, float) == 4.5  # float knob (base backoff)


def test_retry_constants_default_preserved():
    """Tier 2: the retry constants keep their historical defaults when no env override
    is set (1 initial + 2 retries; 2s base) — the env-override is opt-in, byte-compat."""
    import os
    if "REYN_LLM_RETRY_MAX_ATTEMPTS" not in os.environ:
        assert _LLM_RETRY_MAX_ATTEMPTS == 3
    if "REYN_LLM_RETRY_BASE_S" not in os.environ:
        assert _LLM_RETRY_BASE_S == 2.0
