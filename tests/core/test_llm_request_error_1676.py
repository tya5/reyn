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

from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.llm.llm import recorded_acompletion
from tests._support.events import collect_events, settle


class _FakeProviderError(Exception):
    """Real fake mirroring a litellm provider exception (no Mock)."""

    def __init__(self, message: str, status_code: int, body, response_text: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.response = SimpleNamespace(text=response_text)


@pytest.fixture(autouse=True)
def _reset_ambient_event_log():
    yield
    set_llm_request_event_log(None)


def _raising_acompletion(message="Boom", status_code=405, body=None, response_text=""):
    async def _fn(**_kwargs):
        raise _FakeProviderError(message, status_code, body, response_text)
    return _fn


async def _run_and_settle(coro, log):
    try:
        return await coro
    finally:
        await settle(log)


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
