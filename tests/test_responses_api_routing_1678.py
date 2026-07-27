"""Tier 2: #1678 / #3288 follow-up — the `/v1/responses` bridge is delegated to
litellm, not implemented by reyn.

History: `reasoning_effort` + `tools` together are only valid on `/v1/responses`
for some reasoning models (owner-confirmed gpt-5.4 405 repro). #1678 made reyn
rewrite the model string itself (`<provider>/responses/<model>`) so litellm would
route to `/v1/responses` while still returning a chat-completions shape. The
#3288 comment thread investigation (2026-07-26/27, offline + one live smoke)
found litellm >= 1.89.3 already ships its own auto-bridge
(`litellm.main.responses_api_bridge_check`, upstream `BerriAI/litellm#23577`,
merged 2026-03-13 — before #1678 was even filed) that requires no `responses/`
prefix from the caller, and that reyn's own provider-allowlist bridge was
strictly WIDER than litellm's (it fired for `o1`/`o3-mini`/any openai+azure
reasoning model, none of which were ever verified to need it) — the same
"too wide" failure shape that silently broke Gemini streaming pre-#3325. Per
owner approval, reyn's manual bridge (`_to_responses_model`,
`_requires_responses_bridge`, `_responses_bridge_providers`, the
`_routed_to_responses` rewrite) was deleted; reyn now passes the resolved
model straight to `litellm.acompletion` unchanged and litellm decides
internally whether to bridge.

`ResponsesEndpointRequiredError` was DELIBERATELY KEPT (owner decision, #3288
comment thread) as the safety net that makes this delegation reversible: since
reyn can no longer tell whether IT applied a bridge, the decision-enabling
error now fires on any 405 for a `tools + reasoning_effort` CALL SHAPE,
regardless of model/provider — the guidance is equally true whether litellm's
own bridge fired or the endpoint simply doesn't serve `/v1/responses`.

No mocks: real `recorded_acompletion`, a real async fake for `litellm.acompletion`
(capturing the model / raising a litellm-shaped 405), monkeypatched.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.llm.llm import ResponsesEndpointRequiredError, recorded_acompletion


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)  # no proxy → model unstripped
    monkeypatch.delenv("LITELLM_API_BASE", raising=False)  # the var proxy_kwargs() actually reads
    yield
    set_llm_request_event_log(None)


def _capturing_acompletion(captured: dict):
    async def _fn(**kwargs):
        captured["model"] = kwargs.get("model")
        return SimpleNamespace(choices=[], usage=None)
    return _fn


# ── reyn no longer rewrites the model string — litellm sees the bare model ──────


def test_openai_reasoning_combo_model_passed_through_unchanged(monkeypatch) -> None:
    """Tier 2: an OpenAI reasoning-shaped call (tools + reasoning_effort, the
    #1678 bug shape) reaches `litellm.acompletion` with the model UNCHANGED —
    no `responses/` prefix. Delegation means litellm's own internal
    `responses_api_bridge_check` decides whether to route to `/v1/responses`;
    reyn is not in that decision anymore."""
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _capturing_acompletion(captured))

    asyncio.run(recorded_acompletion(
        model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        extra_kwargs={"tools": [{"type": "function"}], "reasoning_effort": "low"},
    ))
    assert captured["model"] == "openai/gpt-5.4"


def test_gemini_combo_model_passed_through_unchanged(monkeypatch) -> None:
    """Tier 2: #3288 regression guard — a Gemini tools+reasoning_effort call
    (the exact default-config primary-reply shape) reaches
    `litellm.acompletion` with the model UNCHANGED. This was the #3288
    default-config streaming bug: reyn's own bridge used to rewrite this to
    a `responses/`-prefixed string `_streaming_capable` could not recognize.
    Deletion removes the rewrite for EVERY model, not just Gemini, so this
    is now unconditionally true rather than provider-gated."""
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _capturing_acompletion(captured))

    asyncio.run(recorded_acompletion(
        model="gemini/gemini-2.5-flash-lite", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        extra_kwargs={"tools": [{"type": "function"}], "reasoning_effort": "low"},
    ))
    assert captured["model"] == "gemini/gemini-2.5-flash-lite"


def test_litellm_native_bridge_check_fires_for_reyn_verified_bug_shape() -> None:
    """Tier 1: OFFLINE evidence that litellm's OWN bridge would engage for the
    exact call shape reyn used to hand-roll a fix for (owner-confirmed gpt-5.4
    + tools + reasoning_effort 405). Calls litellm's real
    `responses_api_bridge_check` directly — no network, no reyn code — the
    same function the #3288 comment thread investigation used to determine
    litellm 1.89.3 already handles this natively. This does NOT prove the
    downstream call succeeds end-to-end (that needs a live `/v1/responses`-
    capable proxy, out of scope here — see the PR body's "unverified" list);
    it proves litellm's bridge decision fires, which is the half of the claim
    this suite can verify offline."""
    from litellm.main import responses_api_bridge_check

    decision, model = responses_api_bridge_check(
        model="gpt-5.4",
        custom_llm_provider="openai",
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        reasoning_effort="low",
    )
    assert decision.get("mode") == "responses"
    assert model == "gpt-5.4"  # no reyn-side prefix needed


# ── decision-enabling error on a 405 for the tools+reasoning_effort shape ───────


class _FakeProviderError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code
        self.body = None
        self.response = SimpleNamespace(text=message)


def test_openai_shaped_405_raises_decision_enabling_error(monkeypatch) -> None:
    """Tier 2: a tools+reasoning_effort call against an OpenAI-family model
    that 405s raises the decision-enabling `ResponsesEndpointRequiredError`
    naming BOTH remedies. Strip the `_needs_responses_endpoint` condition out
    of the 405 handler in `recorded_acompletion` (revert to unconditional
    re-raise) and this goes RED (the raw `_FakeProviderError` propagates
    instead)."""
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


def test_gemini_shaped_405_also_raises_decision_enabling_error(monkeypatch) -> None:
    """Tier 2: owner decision (#3288 comment thread) — the guidance fires on
    ANY `tools + reasoning_effort` call shape that 405s, regardless of
    provider, because reyn can no longer tell whether litellm applied its own
    bridge for this specific model. A Gemini call is included: if it 405s
    while shaped this way, the same guidance is offered rather than a raw
    405. This is a deliberate behavior WIDENING versus the pre-deletion,
    provider-gated error (#3325) — see the PR body for the rationale."""
    async def _raise_405(**_kwargs):
        raise _FakeProviderError("Method Not Allowed", 405)
    monkeypatch.setattr(litellm, "acompletion", _raise_405)

    with pytest.raises(ResponsesEndpointRequiredError):
        asyncio.run(recorded_acompletion(
            model="gemini/gemini-2.5-flash-lite", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            extra_kwargs={"tools": [{"type": "function"}], "reasoning_effort": "low"},
        ))


def test_405_without_tools_or_reasoning_effort_not_wrapped(monkeypatch) -> None:
    """Tier 2: non-vacuity companion — a 405 on a call NOT shaped
    tools+reasoning_effort propagates raw, unwrapped. Proves the gate is
    shape-conditioned, not unconditional on every 405."""
    async def _raise_405(**_kwargs):
        raise _FakeProviderError("Method Not Allowed", 405)
    monkeypatch.setattr(litellm, "acompletion", _raise_405)

    with pytest.raises(_FakeProviderError):
        asyncio.run(recorded_acompletion(
            model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            extra_kwargs={"reasoning_effort": "low"},  # no tools
        ))


def test_405_still_captures_raw_via_1676(monkeypatch) -> None:
    """Tier 2: #1676 still captures the raw 405 detail (status code, body)
    before the decision-enabling error is raised — complementary, not
    replaced by the wrap."""
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
