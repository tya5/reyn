"""Tier 2: #1669 — the `llm_request` P6 observability event at the LLM chokepoint.

`recorded_acompletion` (the single `litellm.acompletion` chokepoint, #1190
AST-guarded) emits a P6 `llm_request` event carrying the NON-message call params
so they are TUI-verifiable when testing a model (reasoning_effort, temperature,
extra_body, response_format, …). The event is delivered via a session-scoped
ambient `EventLog` (ContextVar set by the chat session / kernel runtime); when
unset (tests / dogfood / CLI) the chokepoint skips the emit, mirroring the
`recorder=None` graceful path.

Contract pinned here:
  - the event fires at the chokepoint when the ambient EventLog is set (= the
    ContextVar read propagates — lead's propagation verify);
  - `messages` is excluded; `tools` → `tools_count`; secret-like fields redacted;
  - `response_format` is reflected even though it is applied inside `_once`;
  - ambient EventLog unset → no event (graceful skip).

No mocks: a real `EventLog`, the real redaction helper, and a real async fake for
`litellm.acompletion` (monkeypatched — the documented replay seam).
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from reyn.events.events import (
    EventLog,
    get_llm_request_event_log,
    set_llm_request_event_log,
)
from reyn.llm.llm import _redact_llm_request_params, recorded_acompletion


@pytest.fixture(autouse=True)
def _reset_ambient_event_log():
    """Clear the ambient LLM-request EventLog after each test so the ContextVar
    does not leak across tests in the same process."""
    yield
    set_llm_request_event_log(None)


async def _fake_acompletion(**_kwargs):
    """Real async fake for litellm.acompletion — returns a minimal object. The
    chokepoint only parses usage when recorder is given (None here), so the shape
    is irrelevant to the event-emit path under test."""
    return object()


def _call(monkeypatch, **extra_kwargs):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)  # no proxy → model unstripped
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    return asyncio.run(
        recorded_acompletion(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "secret message body"}],
            purpose="main",
            recorder=None,
            response_format={"type": "json_object"},
            extra_kwargs=extra_kwargs,
        )
    )


# ── the event fires + carries the non-message params ───────────────────────────


def test_event_emitted_with_non_message_params(monkeypatch) -> None:
    """Tier 2: #1669 — with the ambient EventLog set, a chokepoint call emits one
    `llm_request` event carrying model / purpose / the non-message params (proves
    the ContextVar read propagates into the call — lead's propagation verify)."""
    log = EventLog()
    set_llm_request_event_log(log)

    _call(
        monkeypatch,
        reasoning_effort="low",
        temperature=0.7,
        extra_body={"thinking": {"budget": 1024}},
    )

    kinds = [e.type for e in log.all()]
    assert kinds == ["llm_request"], f"expected exactly one llm_request; got {kinds}"
    data = log.all()[0].data
    assert data["model"] == "gpt-5.4"
    assert data["purpose"] == "main"
    assert data["params"]["reasoning_effort"] == "low"
    assert data["params"]["temperature"] == 0.7
    assert data["params"]["extra_body"] == {"thinking": {"budget": 1024}}
    # response_format is applied inside _once but still reflected in the event.
    assert data["params"]["response_format"] == {"type": "json_object"}


def test_event_excludes_messages(monkeypatch) -> None:
    """Tier 2: #1669 — the event NEVER carries `messages` (owner: 'メッセージ以外').
    The secret message body must not leak anywhere in the event data."""
    log = EventLog()
    set_llm_request_event_log(log)

    _call(monkeypatch, temperature=0.1)

    data = log.all()[0].data
    assert "messages" not in data
    assert "messages" not in data["params"]
    assert "secret message body" not in repr(data)


def test_event_tools_count_not_array(monkeypatch) -> None:
    """Tier 2: #1669 — `tools` is surfaced as a count, never the (large) array."""
    log = EventLog()
    set_llm_request_event_log(log)

    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(18)]
    _call(monkeypatch, tools=tools, temperature=0.0)

    data = log.all()[0].data
    assert data["tools_count"] == 18
    assert "tools" not in data["params"], "the tools array must not be in the event"


def test_event_redacts_secret_fields(monkeypatch) -> None:
    """Tier 2: #1669 — secret-like params (api_key / authorization) are redacted."""
    log = EventLog()
    set_llm_request_event_log(log)

    _call(monkeypatch, api_key="sk-super-secret", authorization="Bearer tok-123")

    params = log.all()[0].data["params"]
    assert params["api_key"] == "***REDACTED***"
    assert params["authorization"] == "***REDACTED***"
    assert "sk-super-secret" not in repr(params)
    assert "tok-123" not in repr(params)


def test_no_event_when_ambient_log_unset(monkeypatch) -> None:
    """Tier 2: #1669 — with NO ambient EventLog (tests / dogfood / CLI), the
    chokepoint skips the emit (graceful, mirrors recorder=None). The call still
    completes normally."""
    set_llm_request_event_log(None)
    assert get_llm_request_event_log() is None

    # Should not raise even though there is no sink.
    result = _call(monkeypatch, temperature=0.5)
    assert result is not None


# ── the redaction helper (pure) ────────────────────────────────────────────────


def test_redact_helper_drops_tools_and_messages_keeps_rest() -> None:
    """Tier 2: #1669 — the pure redaction helper drops tools/messages, redacts
    secrets, keeps the rest, and folds in response_format."""
    out = _redact_llm_request_params(
        {
            "tools": [1, 2, 3],
            "messages": ["leak"],
            "reasoning_effort": "high",
            "API_KEY": "x",  # case-insensitive
            "num_retries": 2,
        },
        response_format={"type": "json_object"},
    )
    assert "tools" not in out and "messages" not in out
    assert out["reasoning_effort"] == "high"
    assert out["num_retries"] == 2
    assert out["API_KEY"] == "***REDACTED***"
    assert out["response_format"] == {"type": "json_object"}


def test_redact_helper_omits_response_format_when_none() -> None:
    """Tier 2: #1669 — response_format absent when None (not forced into params)."""
    out = _redact_llm_request_params({"temperature": 0.3}, response_format=None)
    assert "response_format" not in out
    assert out["temperature"] == 0.3
