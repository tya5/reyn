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
error fires on a 405 for a `tools + reasoning_effort` CALL SHAPE — the
guidance is equally true whether litellm's own bridge fired or the endpoint
simply doesn't serve `/v1/responses`.

**Provider-scoped (#3331 co-vet finding).** The first version of this PR
scoped the trigger on call shape ALONE, regardless of provider. Co-vet caught
that this makes the guidance categorically FALSE for a Gemini 405 (litellm's
own bridge — `litellm.main.responses_api_bridge_check`, read directly from
its source — only ever fires for `custom_llm_provider in ("openai",
"azure")`; a Gemini 405 is unrelated to `/v1/responses` regardless of call
shape). `_may_need_responses_endpoint(model)` reuses the SAME
`litellm.get_llm_provider` derivation the now-deleted
`_requires_responses_bridge` used, but ONLY to scope this diagnostic — never
again to rewrite the model. The message also weakened "requires" → "MAY
require", since reyn no longer controls routing and can only say the shape
COULD have needed the bridge.

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
    _may_need_responses_endpoint,
    recorded_acompletion,
)
from tests._support.events import collect_events


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
    a `responses/`-prefixed string `_streaming_capability` could not recognize.
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


def test_may_need_responses_endpoint_scoped_to_openai_and_azure() -> None:
    """Tier 1: `_may_need_responses_endpoint` mirrors litellm's OWN
    `responses_api_bridge_check` provider gate (`custom_llm_provider in
    ("openai", "azure")`, read directly from litellm's source) — true for
    both providers litellm ever bridges, false for a provider it never
    does."""
    assert _may_need_responses_endpoint("openai/gpt-5.4") is True
    assert _may_need_responses_endpoint("azure/o1") is True
    assert _may_need_responses_endpoint("gemini/gemini-2.5-flash-lite") is False
    assert _may_need_responses_endpoint("anthropic/claude-opus-4-1") is False


# ── decision-enabling error on a 405 for the tools+reasoning_effort shape ───────


class _FakeProviderError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code
        self.body = None
        self.response = SimpleNamespace(text=message)


def test_openai_shaped_405_raises_decision_enabling_error(monkeypatch) -> None:
    """Tier 2: ★POSITIVE — a tools+reasoning_effort call against an
    OpenAI-family model that 405s raises the decision-enabling
    `ResponsesEndpointRequiredError` naming BOTH remedies, with "MAY require"
    wording (reyn no longer controls routing, so it can only say the shape
    COULD have needed the bridge). Strip the provider condition out of
    `_may_need_responses_endpoint` (force it to always return `True`) and
    this test alone would NOT go red (openai already resolves True) — see
    `test_gemini_shaped_405_is_not_wrapped_since_provider_unbridgeable` for
    the negative companion that DOES catch an unscoped/wrong provider
    check."""
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
    assert "may require" in msg  # weakened wording — reyn doesn't control routing anymore
    assert "reasoning_effort" in msg and ("none" in msg or "unset" in msg)  # remedy 1
    assert "proxy" in msg  # remedy 2
    assert "gpt-5.4" in str(ei.value)  # names the model


def test_gemini_shaped_405_is_not_wrapped_since_provider_unbridgeable(monkeypatch) -> None:
    """Tier 2: ★NEGATIVE — #3331 co-vet finding. A Gemini call shaped
    `tools + reasoning_effort` that 405s is NOT wrapped in
    `ResponsesEndpointRequiredError`: litellm's own bridge
    (`litellm.main.responses_api_bridge_check`) never fires for a non
    openai/azure provider, so claiming "this MAY require /v1/responses" for
    a Gemini 405 would be categorically FALSE guidance, not merely
    imprecise — the raw error must propagate instead. This is the strip
    target: force `_may_need_responses_endpoint` to always return `True`
    (simulating the unscoped, shape-only version co-vet caught) and this
    assertion goes RED (`ResponsesEndpointRequiredError` gets raised for
    Gemini instead of the raw `_FakeProviderError` propagating)."""
    async def _raise_405(**_kwargs):
        raise _FakeProviderError("Method Not Allowed", 405)
    monkeypatch.setattr(litellm, "acompletion", _raise_405)

    with pytest.raises(_FakeProviderError):  # raw error propagates, NOT wrapped
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
    collected = collect_events(log)
    set_llm_request_event_log(log)

    with pytest.raises(ResponsesEndpointRequiredError):
        asyncio.run(recorded_acompletion(
            model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            extra_kwargs={"tools": [{"type": "function"}], "reasoning_effort": "low"},
        ))
    (err,) = [e for e in collected if e.type == "llm_request_error"]
    assert err.data["status_code"] == 405
