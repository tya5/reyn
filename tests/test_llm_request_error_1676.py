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


def _call(monkeypatch, **extra_kwargs):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    return asyncio.run(
        recorded_acompletion(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            purpose="main",
            recorder=None,
            extra_kwargs=extra_kwargs,
        )
    )


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
    set_llm_request_event_log(log)

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch, temperature=0.5)

    # Exactly one llm_request_error (unpack-enforcement).
    (err,) = [e for e in log.all() if e.type == "llm_request_error"]
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
    set_llm_request_event_log(log)

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch)

    (err,) = [e for e in log.all() if e.type == "llm_request_error"]
    # Exact equality proves the whole body survived (no truncation).
    assert err.data["provider_body"] == long_body


def test_error_event_redacts_secret_value(monkeypatch) -> None:
    """Tier 2: #1676 — a secret value (api_key) echoed in the provider error text is
    scrubbed from the captured event (no key leak in the audit log)."""
    monkeypatch.setattr(
        litellm, "acompletion",
        _raising_acompletion(
            message="Auth failed for key sk-supersecret-123",
            status_code=401,
            body="rejected token sk-supersecret-123",
            response_text="sk-supersecret-123",
        ),
    )
    log = EventLog()
    set_llm_request_event_log(log)

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch, api_key="sk-supersecret-123")

    (err,) = [e for e in log.all() if e.type == "llm_request_error"]
    data = err.data
    # The secret must not leak anywhere in the captured error event.
    assert "sk-supersecret-123" not in repr(data), "secret value must be scrubbed"
    assert "***REDACTED***" in data["error_message"]
    # The params dict redacts the api_key by key, too.
    assert data["params"]["api_key"] == "***REDACTED***"


def test_no_event_when_ambient_log_unset_but_still_raises(monkeypatch) -> None:
    """Tier 2: #1676 — with no ambient EventLog (tests / CLI), no event is emitted
    but the exception STILL propagates (capture is best-effort; re-raise is not)."""
    set_llm_request_event_log(None)
    monkeypatch.setattr(litellm, "acompletion", _raising_acompletion())

    with pytest.raises(_FakeProviderError):
        _call(monkeypatch)
