"""Tier 2: OS-invariant tests for #1829 S3a — the #1835 retry-layering fold,
NARROWED further by #5793.

S3a made the litellm.Router own infra-exception retry (with native Retry-After
respect) on the router path, so Reyn's ``_llm_call_with_retry`` no longer
re-retried infra exceptions when the router was ON — dropping to
EmptyLLMResponseError-only there, while Router OFF stayed byte-identical to
pre-#1829 (full exponential-backoff retry of every infra-exception kind).

#5793 (owner decision, "自前の...汎用再試行は litellm に委ねる") removed the OFF-path
half of that split: ``_is_retryable_exc`` now classifies ONLY
EmptyLLMResponseError as retryable, on EVERY path — litellm's own
``num_retries`` (passed to ``acompletion``/the Router alike) is the infra-retry
layer now, not a router-state-conditional reyn one. The router-ON/OFF
DISTINCTION this file used to test is gone: an infra exception is never
re-retried by reyn any more, regardless of ``REYN_LLM_USE_ROUTER``.
EmptyLLMResponseError stays Reyn-owned on both paths, unaffected by #5793 (the
Router never retries a non-exception 200 either way).

Policy: no mocks of collaborators — the real ``_llm_call_with_retry`` is driven by
a real async callable and the real env gate (``REYN_LLM_USE_ROUTER``); only the
backoff *timer* (our own ``_backoff_s`` helper) is neutralised to keep the test
fast (controlling test timing, not faking a contract). Tier line first.
"""
from __future__ import annotations

import litellm
import pytest

import reyn.llm.llm as llm_mod
from reyn.llm.llm import _llm_call_with_retry


class _Resp:
    """Minimal litellm-response stand-in: only ``.choices`` is read by the
    retry wrapper's empty-choices check (and ``.model`` by the diag logger)."""

    def __init__(self, choices: list) -> None:
        self.choices = choices
        self.model = "openai/gpt-4o-mini"


def _infra_exc() -> Exception:
    return litellm.InternalServerError(
        "boom", model="openai/gpt-4o-mini", llm_provider="openai"
    )


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the retry sleep so a real-retry path doesn't wait 2s/4s."""
    monkeypatch.setattr(llm_mod, "_backoff_s", lambda attempt: 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("router_env", ["", "1"])
async def test_infra_exception_never_retried_by_reyn_regardless_of_router(
    monkeypatch: pytest.MonkeyPatch, router_env: str,
) -> None:
    """Tier 2: #5793 — an infra exception propagates on the FIRST attempt,
    whether ``REYN_LLM_USE_ROUTER`` is unset (OFF, the historical
    always-retried path pre-#5793) or set (ON). The router-state
    distinction S3a used to carry is gone: litellm's own ``num_retries``
    is the infra-retry layer now, unconditionally — not "reyn retries
    when OFF, Router retries when ON"."""
    if router_env:
        monkeypatch.setenv("REYN_LLM_USE_ROUTER", router_env)
    else:
        monkeypatch.delenv("REYN_LLM_USE_ROUTER", raising=False)
    calls = {"n": 0}

    async def coro_fn() -> object:
        calls["n"] += 1
        raise _infra_exc()

    with pytest.raises(litellm.InternalServerError):
        await _llm_call_with_retry(coro_fn, "openai/gpt-4o-mini", None)
    assert calls["n"] == 1, (
        f"an infra exception must not be re-retried by reyn (router_env={router_env!r}) "
        "— litellm's own num_retries is the retry layer now, on every path"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("router_env", ["", "1"])
async def test_empty_choices_still_retried_by_reyn_regardless_of_router(
    monkeypatch: pytest.MonkeyPatch, router_env: str,
) -> None:
    """Tier 2: EmptyLLMResponseError (200 + empty choices, #187 B1) stays
    Reyn-owned and is STILL retried on EVERY path (unaffected by #5793 —
    litellm structurally cannot retry a non-exception 200, on OR off the
    router)."""
    if router_env:
        monkeypatch.setenv("REYN_LLM_USE_ROUTER", router_env)
    else:
        monkeypatch.delenv("REYN_LLM_USE_ROUTER", raising=False)
    calls = {"n": 0}

    async def coro_fn() -> object:
        calls["n"] += 1
        if calls["n"] <= 2:
            return _Resp([])  # 200 + empty choices → EmptyLLMResponseError
        return _Resp([object()])

    resp = await _llm_call_with_retry(coro_fn, "openai/gpt-4o-mini", None)
    assert resp.choices, f"router_env={router_env!r} must still retry the empty-choices condition"
    assert calls["n"] == 3, (
        f"EmptyLLMResponseError (#187 B1) is Reyn-owned regardless of router "
        f"state (router_env={router_env!r}; 2 empty + 1 ok)"
    )
