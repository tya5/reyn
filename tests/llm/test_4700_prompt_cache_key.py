"""Tier 2: #4700 — reyn sends OpenAI's ``prompt_cache_key`` (session-unit
granularity), where before it sent nothing and every call fell back to
prefix-hash-only routing.

Mirrors ``test_reasoning_effort_config_1650.py``'s own shape exactly (the
established precedent for "a per-call litellm param that must be threaded
AND whitelisted for proxy passthrough"): a real async capturing litellm
stub records the kwargs ``call_llm_tools`` actually hands to
``litellm.acompletion`` — no mocks, the litellm BOUNDARY itself is the
thing under test, not reyn's own intent to send it.

``RouterLoop._prompt_cache_key()`` (the session-unit read) is covered
directly against a real ``RouterHostAdapter`` (``tests/_support/
router_host_adapter.make_adapter``), not a fake — ``live_session_id`` is a
real property on the real adapter class.
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelSpec
from reyn.llm.pricing import TokenUsage


class _CapturingFinishLLM:
    """Real callable (testing policy, mirrors test_force_close_trigger_1092.py's
    own class of the same name): records the kwargs of the LAST call and
    returns a finish (no tool_calls) so RouterLoop.run terminates after one
    round — a single, unambiguous capture."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_kwargs: dict = {}

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.call_count += 1
        self.last_kwargs = kwargs
        return LLMToolCallResult(
            content="ok", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )


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


# ── Tier 2: the value ARRIVES at the litellm boundary (not dropped) ─────────


@pytest.mark.asyncio
async def test_prompt_cache_key_arrives_at_litellm_call(monkeypatch):
    """Tier 2: #4700 — a prompt_cache_key passed to call_llm_tools threads
    through the REAL call chain and ARRIVES in the kwargs handed to
    litellm.acompletion. RED without the fix: the key never reaches the
    stub's captured kwargs."""
    import litellm

    from reyn.llm.llm import call_llm_tools

    stub = _CapturingLLM()
    monkeypatch.setattr(litellm, "acompletion", stub)

    await call_llm_tools(
        model=ModelSpec(model="gemini/gemini-2.5-flash-lite", kwargs={}),
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_retries=0,
        prompt_cache_key="session-abc123",
    )

    assert stub.calls, "litellm.acompletion was never reached"
    assert stub.calls[0].get("prompt_cache_key") == "session-abc123", (
        f"prompt_cache_key was dropped before the litellm call; "
        f"got kwargs keys: {sorted(stub.calls[0])}"
    )


@pytest.mark.asyncio
async def test_prompt_cache_key_whitelisted_for_proxy_passthrough(monkeypatch):
    """Tier 2: #4700 — on the openai-compat PROXY path (reyn's default
    routing), prompt_cache_key must be whitelisted via allowed_openai_params
    or a DIRECT-provider route (#309, e.g. an operator's `provider: gemini`
    model class bypassing the proxy) would have litellm reject it as an
    unsupported param before ever reaching the wire (UnsupportedParamsError
    — confirmed live against the installed litellm: `get_optional_params
    (model=..., custom_llm_provider="gemini", prompt_cache_key=...)` raises
    without this whitelist, passes with it; caught by that live check, not
    by this monkeypatch, which bypasses litellm's own validation — same
    caveat `test_reasoning_effort_whitelisted_for_proxy_passthrough` states
    for its own sibling param)."""
    import litellm

    from reyn.llm.llm import call_llm_tools

    stub = _CapturingLLM()
    monkeypatch.setattr(litellm, "acompletion", stub)

    await call_llm_tools(
        model=ModelSpec(model="gemini/gemini-2.5-flash-lite", kwargs={}),
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_retries=0,
        prompt_cache_key="session-abc123",
    )

    assert stub.calls, "litellm.acompletion was never reached"
    kw = stub.calls[0]
    assert "prompt_cache_key" in (kw.get("allowed_openai_params") or []), (
        f"prompt_cache_key not whitelisted for proxy/direct-provider "
        f"forwarding; allowed_openai_params={kw.get('allowed_openai_params')!r}"
    )


@pytest.mark.asyncio
async def test_no_prompt_cache_key_is_unaffected(monkeypatch):
    """Tier 2: (accept-side) #4700 — no prompt_cache_key passed (the default,
    every pre-#4700 call site) means NOTHING is sent — byte-identical to
    before this feature, not an empty-string / None literal riding the
    wire."""
    import litellm

    from reyn.llm.llm import call_llm_tools

    stub = _CapturingLLM()
    monkeypatch.setattr(litellm, "acompletion", stub)

    await call_llm_tools(
        model=ModelSpec(model="gemini/gemini-2.5-flash-lite", kwargs={}),
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_retries=0,
    )

    assert stub.calls, "litellm.acompletion was never reached"
    assert "prompt_cache_key" not in stub.calls[0]


# ── Tier 2: RouterLoop reads SESSION-unit granularity ────────────────────────


@pytest.mark.asyncio
async def test_router_loop_prompt_cache_key_reads_live_session_id():
    """Tier 2: #4700 — RouterLoop._prompt_cache_key() reads the real
    RouterHostAdapter's live_session_id property (session-unit granularity,
    the ratified design — see llm.py's recorded_acompletion inline comment
    for the A/B measurement this decision is based on). Driven through the
    PUBLIC ``RouterLoop.run`` surface (a real turn's captured LLM-call
    kwargs), not the private helper directly (testing.md's private-state
    ban)."""
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.router_loop import RouterLoop
    from tests._support.router_host_adapter import make_adapter

    host = make_adapter(
        session_id="session-xyz-789", universal_wrappers_enabled=False,
        resolver=ModelResolver({"standard": "openai/gpt-4o"}),
    )
    llm = _CapturingFinishLLM()
    await RouterLoop(host=host, chain_id="c1", llm_caller=llm).run("hi", [])

    assert llm.last_kwargs.get("prompt_cache_key") == "session-xyz-789"


@pytest.mark.asyncio
async def test_router_loop_prompt_cache_key_none_when_host_has_no_live_session_id():
    """Tier 2: (accept-side) a test host without a live session id (the
    common test-construction shape, ``FakeRouterHost`` — no
    ``live_session_id`` attribute at all) passes prompt_cache_key=None —
    getattr-guarded, never a raise. ``call_llm_tools`` itself always names
    the kwarg (its own default is ``None``); it is the DEEPER
    ``recorded_acompletion`` boundary (see the earlier litellm-stub tests
    above) that omits it from the wire entirely when falsy — the layer this
    test observes (an injected ``llm_caller``, standing in for
    ``call_llm_tools`` itself) sees every one of that function's named
    params, by construction."""
    from reyn.runtime.router_loop import RouterLoop
    from tests._support.router_loop import FakeRouterHost

    host = FakeRouterHost()  # no live_session_id attribute
    llm = _CapturingFinishLLM()
    await RouterLoop(host=host, chain_id="c1", llm_caller=llm).run("hi", [])

    assert llm.last_kwargs.get("prompt_cache_key") is None
