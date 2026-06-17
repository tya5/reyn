"""Tier 2: #1678 — route reasoning_effort + tools to the OpenAI Responses API.

`reasoning_effort` + `tools` together are only valid on `/v1/responses`, but reyn
sent everything via `acompletion` (`/chat/completions`) → 405 (owner-confirmed:
removing reasoning_effort cleared it). The fix routes that combo through the
`openai/responses/<model>` bridge prefix (returns a chat-completions shape →
parsing unchanged, no parallel chokepoint). When the proxy/endpoint does not serve
`/v1/responses`, the routed call 405s → reyn raises a decision-enabling error
(naming BOTH remedies) instead of a raw dead-end; #1676 still captures the raw 405.

No mocks: real `recorded_acompletion`, a real async fake for `litellm.acompletion`
(capturing the model / raising a litellm-shaped 405), monkeypatched.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.llm.llm import (
    ResponsesEndpointRequiredError,
    _to_responses_model,
    recorded_acompletion,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)  # no proxy → model unstripped
    yield
    set_llm_request_event_log(None)


# ── the model-string transform ──────────────────────────────────────────────────


def test_to_responses_model_inserts_after_provider() -> None:
    """Tier 2: #1678 — insert the `responses/` bridge marker after the provider
    (direct path) or prepend it (proxy post-strip), idempotent for an already-routed
    model."""
    assert _to_responses_model("openai/gpt-5.4") == "openai/responses/gpt-5.4"
    assert _to_responses_model("gpt-5.4") == "responses/gpt-5.4"
    # idempotent — an explicit operator prefix is preserved (no double-prefix).
    assert _to_responses_model("openai/responses/gpt-5.4") == "openai/responses/gpt-5.4"
    assert _to_responses_model("responses/gpt-5.4") == "responses/gpt-5.4"


# ── routing applied only on (tools + reasoning_effort) ──────────────────────────


def _capturing_acompletion(captured: dict):
    async def _fn(**kwargs):
        captured["model"] = kwargs.get("model")
        return SimpleNamespace(choices=[], usage=None)
    return _fn


def test_combo_routes_to_responses(monkeypatch) -> None:
    """Tier 2: #1678 — a tools + reasoning_effort call is routed: litellm.acompletion
    receives the `responses/`-bridged model (→ /v1/responses)."""
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _capturing_acompletion(captured))

    asyncio.run(recorded_acompletion(
        model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        extra_kwargs={"tools": [{"type": "function"}], "reasoning_effort": "low"},
    ))
    assert captured["model"] == "openai/responses/gpt-5.4"


def test_no_tools_not_routed(monkeypatch) -> None:
    """Tier 2: #1678 — reasoning_effort WITHOUT tools is NOT routed (normal path
    unaffected — the 405 only occurs for the combo)."""
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _capturing_acompletion(captured))

    asyncio.run(recorded_acompletion(
        model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        extra_kwargs={"reasoning_effort": "low"},  # no tools
    ))
    assert captured["model"] == "openai/gpt-5.4", "must NOT be routed without tools"


def test_no_reasoning_not_routed(monkeypatch) -> None:
    """Tier 2: #1678 — tools WITHOUT reasoning_effort is NOT routed."""
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _capturing_acompletion(captured))

    asyncio.run(recorded_acompletion(
        model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        extra_kwargs={"tools": [{"type": "function"}]},  # no reasoning_effort
    ))
    assert captured["model"] == "openai/gpt-5.4", "must NOT be routed without reasoning_effort"


# ── decision-enabling error on a /responses 405 ─────────────────────────────────


class _FakeProviderError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code
        self.body = None
        self.response = SimpleNamespace(text=message)


def test_responses_405_raises_decision_enabling_error(monkeypatch) -> None:
    """Tier 2: #1678 — when reyn routed the combo to /responses and it 405s (proxy
    doesn't serve it), a decision-enabling error is raised naming BOTH remedies."""
    async def _raise_405(**_kwargs):
        raise _FakeProviderError("Method Not Allowed", 405)
    monkeypatch.setattr(litellm, "acompletion", _raise_405)

    with pytest.raises(ResponsesEndpointRequiredError) as ei:
        asyncio.run(recorded_acompletion(
            model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            extra_kwargs={"tools": [{"type": "function"}], "reasoning_effort": "low"},
        ))
    msg = str(ei.value).lower()
    assert "/v1/responses" in str(ei.value)
    assert "reasoning_effort" in msg and ("none" in msg or "unset" in msg)  # remedy 1
    assert "proxy" in msg  # remedy 2
    assert "gpt-5.4" in str(ei.value)  # names the model


def test_responses_405_still_captures_raw_via_1676(monkeypatch) -> None:
    """Tier 2: #1678 — the #1676 llm_request_error event still captures the raw 405
    detail before the decision-enabling error is raised (complementary)."""
    async def _raise_405(**_kwargs):
        raise _FakeProviderError("Method Not Allowed", 405)
    monkeypatch.setattr(litellm, "acompletion", _raise_405)
    log = EventLog()
    set_llm_request_event_log(log)

    with pytest.raises(ResponsesEndpointRequiredError):
        asyncio.run(recorded_acompletion(
            model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            extra_kwargs={"tools": [{"type": "function"}], "reasoning_effort": "low"},
        ))
    (err,) = [e for e in log.all() if e.type == "llm_request_error"]
    assert err.data["status_code"] == 405


def test_non_routed_405_is_not_wrapped(monkeypatch) -> None:
    """Tier 2: #1678 — a 405 on a call reyn did NOT route (no combo) is NOT wrapped
    in the responses-error (the wrap only fires when reyn applied the route)."""
    async def _raise_405(**_kwargs):
        raise _FakeProviderError("Method Not Allowed", 405)
    monkeypatch.setattr(litellm, "acompletion", _raise_405)

    with pytest.raises(_FakeProviderError):  # raw error propagates, NOT wrapped
        asyncio.run(recorded_acompletion(
            model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            extra_kwargs={"reasoning_effort": "low"},  # no tools → not routed
        ))
