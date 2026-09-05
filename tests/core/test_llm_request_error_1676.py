"""Tier 2: #1676 — capture LLM-call exceptions as a P6 `llm_request_error` event.

When an LLM call fails (e.g. the owner's persistent 405) the exception detail was
recorded nowhere, so it couldn't be root-caused. `recorded_acompletion` (the single
acompletion chokepoint) now emits a P6 `llm_request_error` carrying the FULL
provider detail (status_code + whole message/body, NOT truncated) — same ambient
EventLog (ContextVar) + model/purpose context as `llm_request` (#1669) — and then
RE-RAISES (never swallows). Secret values are scrubbed from the freeform text.

No mocks: a real `EventLog`, a real async fake for `litellm.acompletion` that
raises a litellm-shaped exception (.status_code / .body / .response), monkeypatched
on the module (the documented replay seam).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

import reyn.llm.llm
from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.llm.llm import recorded_acompletion
from tests._support.events import collect_events, settle


class _FakeProviderError(Exception):
    """Real fake mirroring a litellm provider exception (no Mock)."""

    def __init__(self, message: str, status_code: "int | None", body, response_text: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.response = SimpleNamespace(text=response_text)


@pytest.fixture(autouse=True)
def _reset_ambient_event_log():
    yield
    set_llm_request_event_log(None)


def _raising_acompletion(
    message="Boom", status_code: "int | None" = 405, body=None, response_text="",
):
    async def _fn(**_kwargs):
        raise _FakeProviderError(message, status_code, body, response_text)
    return _fn


async def _run_and_settle(coro, log):
    try:
        return await coro
    finally:
        await settle(log)


class _FakeClock:
    """#5797: a real, deterministic stand-in for the ``time`` module's own
    ``monotonic()`` -- not a Mock, a plain object with the one method
    ``llm.py`` actually calls. Rebinds ``reyn.llm.llm``'s own ``time`` NAME
    (not the shared ``time`` module's ``monotonic`` attribute in place,
    which would also break asyncio's own event-loop clock -- confirmed the
    hard way: a global monkeypatch of ``time.monotonic`` hangs the test
    runner)."""

    def __init__(self, *values: float) -> None:
        self._it = iter(values)

    def monotonic(self) -> float:
        return next(self._it)


def _inject_monotonic_clock(monkeypatch, *values: float) -> None:
    """#5797: per CLAUDE.md's testing policy, a duration-carrying test
    supplies the clock as an input rather than waiting out a real
    ``sleep()`` the assertion would then depend on as a floor."""
    monkeypatch.setattr(reyn.llm.llm, "time", _FakeClock(*values))


def _call(monkeypatch, log=None, **extra_kwargs):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    coro = recorded_acompletion(
        model="gpt-5.4",
        messages=[{"role": "user", "content": "hi"}],
        purpose="main",
        model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
        recorder=None,
        extra_kwargs=extra_kwargs,
    )
    if log is not None:
        coro = _run_and_settle(coro, log)
    return asyncio.run(coro)


# ── the event fires with full detail + the exception still propagates ───────────


def test_error_event_emitted_and_reraised(monkeypatch) -> None:
    """Tier 2: #1676 — a failing call emits one llm_request_error with the full
    provider detail (status_code + message) AND the exception propagates (the call
    is never silently swallowed)."""
    monkeypatch.setattr(
        litellm, "acompletion",
        _raising_acompletion(message="Method Not Allowed", status_code=405,
                             body={"error": "method not allowed on /v1/chat"},
                             response_text="405 page"),
    )
    log = EventLog()
    collected = collect_events(log)
    set_llm_request_event_log(log)

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch, log=log, temperature=0.5)

    # Exactly one llm_request_error (unpack-enforcement).
    (err,) = [e for e in collected if e.type == "llm_request_error"]
    data = err.data
    assert data["model"] == "gpt-5.4"
    assert data["purpose"] == "main"
    assert data["error_type"] == "_FakeProviderError"
    assert "Method Not Allowed" in data["error_message"]
    assert data["status_code"] == 405
    # The whole provider body survives (root-cause signal — not truncated).
    assert data["provider_body"] == {"error": "method not allowed on /v1/chat"}


def test_error_body_not_truncated(monkeypatch) -> None:
    """Tier 2: #1676 — a long provider body is captured WHOLE (the 405's body is
    the root-cause signal; truncating it would lose the detail)."""
    long_body = "X" * 5000 + "ROOT_CAUSE_MARKER"
    monkeypatch.setattr(
        litellm, "acompletion",
        _raising_acompletion(message="err", status_code=400, body=long_body,
                             response_text=""),
    )
    log = EventLog()
    collected = collect_events(log)
    set_llm_request_event_log(log)

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch, log=log)

    (err,) = [e for e in collected if e.type == "llm_request_error"]
    # Exact equality proves the whole body survived (no truncation).
    assert err.data["provider_body"] == long_body


# #3830-follow-up (removed, not fixed): the two tests that lived here
# (`test_error_event_redacts_secret_value`,
# `test_error_event_redacts_secret_value_in_dict_shaped_body`) asserted that
# `reyn.llm.secret_scrub` scrubbed a fixture-injected `api_key` kwarg out of
# the captured error event. That module is gone (#3830-follow-up: reyn never
# had a documented motive to inspect key values, and multi-layer defense
# there was never sound — scrubbing requires reyn to KNOW the secret value,
# which conflicts with the owner's standing "API KEY is proxy/litellm's
# responsibility, never inspect" instruction). Since #4348, reyn no longer
# passes an `api_key` kwarg into `recorded_acompletion` at all — these tests
# constructed a call shape (`_call(monkeypatch, api_key="...")`) that
# production no longer builds, so their premise did not survive the removal
# and they were deleted rather than re-pointed at a mechanism that no
# longer exists.


def test_no_event_when_ambient_log_unset_but_still_raises(monkeypatch) -> None:
    """Tier 2: #1676 — with no ambient EventLog (tests / CLI), no event is emitted
    but the exception STILL propagates (capture is best-effort; re-raise is not)."""
    set_llm_request_event_log(None)
    monkeypatch.setattr(litellm, "acompletion", _raising_acompletion())

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch)


# ── #5797: litellm_retries + elapsed_s ──────────────────────────────────────


def test_error_event_parses_litellm_retry_count_from_exception_text(monkeypatch) -> None:
    """Tier 2: #5797 — litellm appends "LiteLLM Retried: N times" to an
    exception's own message once ITS internal retry mechanism (invisible
    to reyn) is exhausted. The owner's own real incident (#5793) carried
    this exact substring; `llm_request_error` now surfaces it as a
    structured `litellm_retries` field instead of leaving it buried in
    free text."""
    monkeypatch.setattr(
        litellm, "acompletion",
        _raising_acompletion(
            message=(
                "litellm.Timeout: APITimeoutError - Request timed out. "
                "timeout value=60.0, time taken=245.2 seconds  "
                "LiteLLM Retried: 3 times"
            ),
            status_code=None,
        ),
    )
    log = EventLog()
    collected = collect_events(log)
    set_llm_request_event_log(log)

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch, log=log)

    (err,) = [e for e in collected if e.type == "llm_request_error"]
    assert err.data["litellm_retries"] == 3


def test_error_event_litellm_retries_none_when_litellm_never_retried(monkeypatch) -> None:
    """Tier 2: #5797 — a straight failure (e.g. a 4xx litellm never retries)
    must NOT claim "0 retries" (a false "litellm tried and gave up
    immediately" claim); the field is `None` -- genuinely absent, not a
    fabricated zero."""
    monkeypatch.setattr(
        litellm, "acompletion",
        _raising_acompletion(message="Method Not Allowed", status_code=405),
    )
    log = EventLog()
    collected = collect_events(log)
    set_llm_request_event_log(log)

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch, log=log)

    (err,) = [e for e in collected if e.type == "llm_request_error"]
    assert err.data["litellm_retries"] is None


def test_error_event_carries_real_elapsed_time(monkeypatch) -> None:
    """Tier 2: #5797 — `elapsed_s` reflects a genuine measurement, not a
    hardcoded stand-in. A real (injected, not slept-out per CLAUDE.md's
    testing policy) clock advancing by exactly 5.5s between the call's
    start and its failure must show up as exactly 5.5 on the event —
    proving the field is wired to a REAL timer read at both ends, not a
    constant."""
    monkeypatch.setattr(litellm, "acompletion", _raising_acompletion())
    _inject_monotonic_clock(monkeypatch, 100.0, 105.5)
    log = EventLog()
    collected = collect_events(log)
    set_llm_request_event_log(log)

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch, log=log)

    (err,) = [e for e in collected if e.type == "llm_request_error"]
    assert err.data["elapsed_s"] == pytest.approx(5.5)
