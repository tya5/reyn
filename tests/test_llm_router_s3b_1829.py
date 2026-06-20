"""Tier 2: OS-invariant tests for #1829 S3b — llm.router.* single-source config,
multi-deployment fallback chain, and cost-records-actual-model.

S3b adds (a) a single-source resolver (``_resolved_router_config`` — reyn.yaml via
the ContextVar is authoritative, the legacy env vars are the fallback; ONE
resolution site so ``_use_llm_router`` and the Router builder never double-source),
(b) a multi-deployment ``litellm.Router`` builder that wires a cross-model
``fallbacks`` chain from ``llm.router.fallbacks``, and (c) cost-records-actual-model
(on a router fallback, attribute cost to ``response.model``, gated so the OFF path
stays byte-identical).

Policy: no mocks of Reyn collaborators — real ``RouterConfig`` + real resolver +
real ``recorded_acompletion`` + a real ``litellm.Router``. ``litellm.acompletion``
is patched only because it IS the replay boundary (the same seam ``LLMReplay``
monkeypatches) — used here to script a deterministic primary-fail → fallback path.
Tier line first; no private-state / count pins beyond the public contract.
"""
from __future__ import annotations

from unittest import mock

import litellm
import pytest

import reyn.llm.llm as llm_mod
from reyn.config.infra import RouterConfig
from reyn.llm.llm import (
    _resolved_router_config,
    _single_deployment_router,
    _use_llm_router,
    recorded_acompletion,
    set_router_config,
)

_PRIMARY = "openai/gpt-4o-mini"
_FALLBACK = "openai/gpt-3.5-turbo"


@pytest.fixture(autouse=True)
def _isolate_router_ctx_and_model_cost(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with NO ambient router config (→ env/default fallback) and
    no router env vars; litellm.model_cost is snapshot/restored (Router build
    registers price-less placeholders — the #1762 global-state class)."""
    monkeypatch.delenv("REYN_LLM_USE_ROUTER", raising=False)
    monkeypatch.delenv("REYN_LLM_ROUTER_NUM_RETRIES", raising=False)
    llm_mod._router_config_var.set(None)
    before = dict(litellm.model_cost)
    yield
    llm_mod._router_config_var.set(None)
    litellm.model_cost.clear()
    litellm.model_cost.update(before)


class _Usage:
    def __init__(self, p: int, c: int) -> None:
        self.prompt_tokens = p
        self.completion_tokens = c


class _Resp:
    def __init__(self, model: str) -> None:
        self.choices = [object()]
        self.model = model
        self.usage = _Usage(10, 5)


class _Recorder:
    def __init__(self) -> None:
        self.models: list[str] = []

    def record_llm(self, *, model, agent, usage, purpose) -> None:
        self.models.append(model)


# ── (a) single-source config resolution ──────────────────────────────────────

def test_contextvar_config_is_authoritative() -> None:
    """Tier 2: reyn.yaml (the ContextVar RouterConfig) is the authoritative source;
    _use_llm_router + num_retries both read it (single source, no env)."""
    set_router_config(RouterConfig(use=True, num_retries=7))
    assert _use_llm_router() is True
    assert _resolved_router_config().num_retries == 7


def test_env_fallback_when_no_contextvar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: with NO reyn.yaml config in context, the legacy env vars are the
    fallback tail of the single-source idiom (back-compat)."""
    monkeypatch.setenv("REYN_LLM_USE_ROUTER", "1")
    monkeypatch.setenv("REYN_LLM_ROUTER_NUM_RETRIES", "4")
    assert _use_llm_router() is True
    assert _resolved_router_config().num_retries == 4


def test_router_off_by_default() -> None:
    """Tier 2: no config + no env → router OFF (the direct litellm.acompletion path)."""
    assert _use_llm_router() is False


# ── (b) multi-deployment fallback chain builder ──────────────────────────────

@pytest.mark.asyncio
async def test_fallback_chain_built_and_fires() -> None:
    """Tier 2: a configured llm.router.fallbacks chain builds a multi-deployment
    Router that actually falls back to the next model when the primary fails."""
    set_router_config(RouterConfig(use=True, num_retries=0, fallbacks={_PRIMARY: [_FALLBACK]}))
    calls: list[str] = []

    async def _fake(*a, **k):
        m = k.get("model")
        calls.append(m)
        if m == _PRIMARY:
            raise litellm.InternalServerError("primary down", model=_PRIMARY, llm_provider="openai")
        return _Resp(m)

    with mock.patch.object(litellm, "acompletion", side_effect=_fake):
        resp = await _single_deployment_router(_PRIMARY).acompletion(
            model=_PRIMARY, messages=[{"role": "user", "content": "x"}],
        )
    assert resp.model == _FALLBACK, "the chain must fall back to the configured model"
    assert _PRIMARY in calls and _FALLBACK in calls, (
        "both the primary attempt and the fallback must route through "
        "litellm.acompletion (replay-compat)"
    )


# ── (c) cost-records-actual-model (+ OFF byte-identical) ──────────────────────

@pytest.mark.asyncio
async def test_router_cache_rebuilds_on_config_change() -> None:
    """Tier 2: (F1) the per-loop Router cache key includes a config fingerprint —
    a changed llm.router.* rebuilds the Router (no silent stale reuse), while the
    same config still hits the cache (the #1762 loop-cache efficiency is kept)."""
    set_router_config(RouterConfig(use=True, num_retries=2))
    r1 = _single_deployment_router(_PRIMARY)
    assert _single_deployment_router(_PRIMARY) is r1, "same config must hit the cache"
    set_router_config(RouterConfig(use=True, num_retries=5))  # config changed
    assert _single_deployment_router(_PRIMARY) is not r1, (
        "a changed config must rebuild the Router, not reuse a stale-config one"
    )


@pytest.mark.asyncio
async def test_cost_records_actual_model_on_fallback() -> None:
    """Tier 2: when a router fallback serves the call, cost is attributed to the
    ACTUAL deployment (response.model), not the requested model."""
    set_router_config(RouterConfig(use=True, num_retries=0, fallbacks={_PRIMARY: [_FALLBACK]}))
    rec = _Recorder()

    async def _fake(*a, **k):
        if k.get("model") == _PRIMARY:
            raise litellm.InternalServerError("down", model=_PRIMARY, llm_provider="openai")
        return _Resp(k.get("model"))

    with mock.patch.object(litellm, "acompletion", side_effect=_fake):
        await recorded_acompletion(
            model=_PRIMARY, messages=[{"role": "user", "content": "x"}],
            purpose="dogfood", recorder=rec,
        )
    assert rec.models == [_FALLBACK], (
        "a fallback must record cost against the model that actually ran, not the "
        f"requested one (got {rec.models!r})"
    )


@pytest.mark.asyncio
async def test_cost_records_requested_model_when_router_off() -> None:
    """Tier 2: OFF path is byte-identical — cost records the REQUESTED model even
    if the (direct) response.model string differs (no actual-model switch)."""
    # router OFF (default). The direct litellm.acompletion path.
    rec = _Recorder()

    async def _fake(*a, **k):
        return _Resp("some/normalized-name")  # response.model != requested

    with mock.patch.object(litellm, "acompletion", side_effect=_fake):
        await recorded_acompletion(
            model=_PRIMARY, messages=[{"role": "user", "content": "x"}],
            purpose="dogfood", recorder=rec,
        )
    assert rec.models == [_PRIMARY], (
        "OFF path must record the requested model (byte-identical), not switch to "
        f"response.model (got {rec.models!r})"
    )
