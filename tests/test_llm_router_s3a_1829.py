"""Tier 2: OS-invariant tests for #1829 S3a — the #1835 retry-layering fold.

S3a makes the litellm.Router own infra-exception retry (with native Retry-After
respect) on the router path, so Reyn's ``_llm_call_with_retry`` no longer
re-retries infra exceptions when the router is ON — it drops to
EmptyLLMResponseError-only (200 + empty choices, #187 B1; the Router never retries
a non-exception 200). Router OFF is byte-identical to pre-#1829 (full
exponential-backoff retry of every ``_is_retryable_exc`` kind).

Policy: no mocks of collaborators — the real ``_llm_call_with_retry`` is driven by
a real async callable and the real env gate (``REYN_LLM_USE_ROUTER``); only the
backoff *timer* (our own ``_backoff_s`` helper) is neutralised to keep the test
fast (controlling test timing, not faking a contract). Tier line first.
"""
from __future__ import annotations

import litellm
import pytest

import reyn.llm.llm as llm_mod
from reyn.llm.llm import EmptyLLMResponseError, _llm_call_with_retry


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
async def test_router_off_infra_exception_still_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: router OFF (default) — an infra exception is retried by Reyn to
    success (the pre-#1829 behavior is preserved, byte-identical)."""
    monkeypatch.delenv("REYN_LLM_USE_ROUTER", raising=False)
    calls = {"n": 0}

    async def coro_fn() -> object:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _infra_exc()
        return _Resp([object()])

    resp = await _llm_call_with_retry(coro_fn, "openai/gpt-4o-mini", None)
    assert resp.choices, "OFF path must retry the infra exception to success"
    assert calls["n"] == 3, "OFF path retries infra exceptions (2 fail + 1 ok)"


@pytest.mark.asyncio
async def test_router_on_infra_exception_not_re_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: router ON — Reyn does NOT re-retry an infra exception (the Router
    owns that retry now); the wrapper raises after a single attempt."""
    monkeypatch.setenv("REYN_LLM_USE_ROUTER", "1")
    calls = {"n": 0}

    async def coro_fn() -> object:
        calls["n"] += 1
        raise _infra_exc()

    with pytest.raises(litellm.InternalServerError):
        await _llm_call_with_retry(coro_fn, "openai/gpt-4o-mini", None)
    assert calls["n"] == 1, (
        "router ON: the Router owns infra-exception retry — Reyn's wrapper must "
        "not re-retry it (would double Router N × Reyn N)"
    )


@pytest.mark.asyncio
async def test_router_on_empty_choices_still_retried_by_reyn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: router ON — EmptyLLMResponseError (200 + empty choices, #187 B1)
    stays Reyn-owned and is STILL retried (the Router never retries a
    non-exception 200)."""
    monkeypatch.setenv("REYN_LLM_USE_ROUTER", "1")
    calls = {"n": 0}

    async def coro_fn() -> object:
        calls["n"] += 1
        if calls["n"] <= 2:
            return _Resp([])  # 200 + empty choices → EmptyLLMResponseError
        return _Resp([object()])

    resp = await _llm_call_with_retry(coro_fn, "openai/gpt-4o-mini", None)
    assert resp.choices, "router ON must still retry the empty-choices condition"
    assert calls["n"] == 3, (
        "EmptyLLMResponseError (#187 B1) is Reyn-owned even on the router path "
        "(2 empty + 1 ok)"
    )


def test_router_baseline_num_retries_default_is_three() -> None:
    """Tier 2: the per-Router baseline retry count defaults to 3 (= today's
    attempt count); a per-call num_retries still overrides it at call time."""
    assert llm_mod._LLM_ROUTER_NUM_RETRIES == 3
