"""#1650: per-model ``reasoning_effort`` reyn.yaml field.

The feature is sugar+validation over the existing ModelSpec.kwargs passthrough:
``reasoning_effort`` declared on a model def lands in ``spec.kwargs`` and rides
the established passthrough to ``litellm.acompletion`` (which maps it to the
provider's native thinking budget — verified live against litellm 1.84.0
gemini). We add load-time validation (fail-fast on a typo) + a both-set reject.

Tiers:
- config-parse round-trip + validation = Tier 1 (the ModelSpec config contract).
- "the non-default value ARRIVES at the litellm boundary, not silently dropped"
  = Tier 2 (the operator-kwargs-reach-the-provider-call invariant; the #1646
  silently-dropped-key lesson).

No mocks: the litellm boundary is a real async callable stub (testing policy),
monkeypatched in, that records the kwargs it receives.
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.llm.model_resolver import (
    VALID_REASONING_EFFORTS,
    ModelResolver,
    ModelSpec,
)

_NON_DEFAULT = "medium"  # not the provider default (thinking off); distinctive


# ── Tier 1: config-parse round-trip ────────────────────────────────────────


def test_reasoning_effort_round_trips_from_config_into_kwargs():
    """Tier 1: #1650 — a model def's ``reasoning_effort`` lands in spec.kwargs
    (the existing passthrough vehicle), round-tripping a NON-DEFAULT value."""
    spec = ModelSpec.from_config(
        {"model": "gemini/gemini-2.5-flash-lite", "reasoning_effort": _NON_DEFAULT}
    )
    assert spec.kwargs.get("reasoning_effort") == _NON_DEFAULT
    assert spec.model == "gemini/gemini-2.5-flash-lite"


def test_reasoning_effort_round_trips_through_resolver_startup():
    """Tier 1: #1650 — the value survives ModelResolver resolution (startup) for
    a tier mapping, the path a real reyn.yaml ``models.light`` takes."""
    r = ModelResolver(
        {"light": {"model": "gemini/gemini-2.5-flash-lite", "reasoning_effort": "low"}}
    )
    assert r.resolve("light").kwargs.get("reasoning_effort") == "low"


# ── Tier 1: load-time validation (fail-fast) ────────────────────────────────


def test_invalid_reasoning_effort_rejected_at_construction():
    """Tier 1: #1650 — an invalid value fails fast at ModelSpec construction
    (config-load), not mid-call inside litellm."""
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        ModelSpec(model="gemini/x", kwargs={"reasoning_effort": "bogus"})


def test_invalid_reasoning_effort_rejected_at_resolver_startup():
    """Tier 1: #1650 — the fail-fast also fires through the resolver startup
    path (a real reyn.yaml typo surfaces at load, with the offending value)."""
    with pytest.raises(ValueError, match="bogus"):
        ModelResolver({"light": {"model": "gemini/x", "reasoning_effort": "bogus"}})


@pytest.mark.parametrize("effort", sorted(VALID_REASONING_EFFORTS))
def test_all_valid_reasoning_efforts_accepted(effort: str):
    """Tier 1: #1650 — every advertised valid value is accepted (no false
    reject narrowing the contract below what litellm supports)."""
    spec = ModelSpec(model="gemini/x", kwargs={"reasoning_effort": effort})
    assert spec.kwargs["reasoning_effort"] == effort


def test_reasoning_effort_and_extra_body_thinking_config_rejected():
    """Tier 1: #1650 — reasoning_effort already maps to a thinking budget, so a
    second hand-set extra_body thinking config is a contradictory control and is
    rejected at load (litellm would otherwise raise on the conflict mid-call)."""
    with pytest.raises(ValueError, match="cannot set both reasoning_effort"):
        ModelSpec(
            model="gemini/x",
            kwargs={
                "reasoning_effort": "low",
                "extra_body": {"thinking_config": {"thinking_budget": 0}},
            },
        )


def test_no_reasoning_effort_is_unaffected():
    """Tier 1: #1650 — a model def without reasoning_effort is unchanged (the
    validation is a no-op; the passthrough policy for other kwargs is intact)."""
    spec = ModelSpec(model="gemini/x", kwargs={"temperature": 0.2})
    assert spec.kwargs == {"temperature": 0.2}


# ── Tier 2: the value ARRIVES at the litellm boundary (not dropped) ─────────


def _fake_litellm_response():
    msg = type("_Msg", (), {"content": "ok", "tool_calls": None})()
    choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
    usage = type("_Usage", (), {"prompt_tokens": 10, "completion_tokens": 5})()
    return type("_Resp", (), {"choices": [choice], "usage": usage})()


class _CapturingLLM:
    """Real async callable stub (testing policy) recording the kwargs it gets."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any):
        self.calls.append(kwargs)
        return _fake_litellm_response()


@pytest.mark.asyncio
async def test_reasoning_effort_arrives_at_litellm_call(monkeypatch):
    """Tier 2: #1650 — a NON-DEFAULT reasoning_effort declared on the model def
    threads through the REAL call_llm_tools (the chat/router path) and ARRIVES in
    the kwargs handed to litellm.acompletion — not silently dropped (#1646
    lesson). The litellm boundary is a real capturing async stub."""
    import litellm

    from reyn.llm.llm import call_llm_tools

    stub = _CapturingLLM()
    monkeypatch.setattr(litellm, "acompletion", stub)

    spec = ModelSpec(
        model="gemini/gemini-2.5-flash-lite",
        kwargs={"reasoning_effort": _NON_DEFAULT},
    )
    await call_llm_tools(
        model=spec,
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_retries=0,
    )

    assert stub.calls, "litellm.acompletion was never reached"
    assert stub.calls[0].get("reasoning_effort") == _NON_DEFAULT, (
        f"reasoning_effort was dropped before the litellm call; "
        f"got kwargs keys: {sorted(stub.calls[0])}"
    )


@pytest.mark.asyncio
async def test_reasoning_effort_whitelisted_for_proxy_passthrough(monkeypatch):
    """Tier 2: #1650 — on the openai-compat PROXY path, reasoning_effort must be
    whitelisted via allowed_openai_params or litellm rejects it as an unsupported
    openai param BEFORE forwarding (UnsupportedParamsError; caught by the live
    proxy smoke, not by the monkeypatch which bypasses litellm validation). This
    pins that the whitelist is threaded so the proxy receives + maps it."""
    import litellm

    from reyn.llm.llm import call_llm_tools

    monkeypatch.setenv("LITELLM_API_BASE", "http://localhost:4000")  # engage proxy path
    stub = _CapturingLLM()
    monkeypatch.setattr(litellm, "acompletion", stub)

    spec = ModelSpec(
        model="gemini/gemini-2.5-flash-lite",
        kwargs={"reasoning_effort": _NON_DEFAULT},
    )
    await call_llm_tools(
        model=spec,
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_retries=0,
    )

    assert stub.calls, "litellm.acompletion was never reached"
    kw = stub.calls[0]
    assert kw.get("reasoning_effort") == _NON_DEFAULT
    assert "reasoning_effort" in (kw.get("allowed_openai_params") or []), (
        f"reasoning_effort not whitelisted for proxy forwarding; the proxy path "
        f"would raise UnsupportedParamsError. allowed_openai_params="
        f"{kw.get('allowed_openai_params')!r}"
    )
