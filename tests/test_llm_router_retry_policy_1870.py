"""Tier 2: OS-invariant tests for #1870 — llm.router.retry_policy typed
litellm.RetryPolicy passthrough.

#1870 adds ``llm.router.retry_policy``: a mapping of per-exception-type retry
counts that is constructed into a ``litellm.RetryPolicy`` and threaded into the
Router builder. Three invariants are pinned:

1. A configured retry_policy reaches the built Router's ``retry_policy``
   attribute with a NON-DEFAULT value so an unwired path cannot silently pass.
2. Absent config → no retry_policy on the Router (default-OFF, no behavior
   change).
3. A retry_policy change invalidates the per-loop Router cache (same invariant
   as num_retries config change, now extended to retry_policy).

Policy: no mocks of Reyn collaborators — real RouterConfig + real resolver +
real litellm.Router. litellm.acompletion is not called here; we only inspect
the Router's public ``retry_policy`` attribute. Tier line first.
"""
from __future__ import annotations

import litellm
import pytest

import reyn.llm.llm as llm_mod
from reyn.config.infra import RouterConfig, _build_router_config
from reyn.llm.llm import (
    _single_deployment_router,
    set_router_config,
)

_M = "openai/gpt-4o-mini"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with NO ambient router config; litellm.model_cost is
    snapshot/restored (Router build registers price-less placeholders)."""
    monkeypatch.delenv("REYN_LLM_USE_ROUTER", raising=False)
    llm_mod._router_config_var.set(None)
    before = dict(litellm.model_cost)
    yield
    llm_mod._router_config_var.set(None)
    litellm.model_cost.clear()
    litellm.model_cost.update(before)


# ── (1) configured retry_policy reaches the Router ───────────────────────────

@pytest.mark.asyncio
async def test_retry_policy_reaches_router() -> None:
    """Tier 2: a configured llm.router.retry_policy constructs a litellm.RetryPolicy
    and threads it to the Router — the Router's retry_policy attribute reflects the
    NON-DEFAULT value (RateLimitErrorRetries=5) so an unwired path cannot pass."""
    set_router_config(RouterConfig(
        use=True,
        retry_policy={"RateLimitErrorRetries": 5},
    ))
    router = _single_deployment_router(_M)
    rp = router.retry_policy
    assert rp is not None, "retry_policy must be set on the Router when configured"
    assert isinstance(rp, litellm.RetryPolicy), (
        f"retry_policy must be a litellm.RetryPolicy, got {type(rp)}"
    )
    assert rp.RateLimitErrorRetries == 5, (
        f"RateLimitErrorRetries must be 5 (non-default), got {rp.RateLimitErrorRetries}"
    )


# ── (2) absent config → no retry_policy on the Router (default-OFF) ──────────

@pytest.mark.asyncio
async def test_absent_retry_policy_not_set_on_router() -> None:
    """Tier 2: when llm.router.retry_policy is absent the Router is built without a
    retry_policy — default-OFF, current behavior is unchanged."""
    set_router_config(RouterConfig(use=True))  # no retry_policy
    router = _single_deployment_router(_M)
    # litellm.Router sets retry_policy=None when not provided
    assert router.retry_policy is None, (
        "retry_policy must not be set when absent from config (default-OFF)"
    )


# ── (3) retry_policy change invalidates the cache ────────────────────────────

@pytest.mark.asyncio
async def test_cache_invalidated_on_retry_policy_change() -> None:
    """Tier 2: a changed retry_policy invalidates the per-loop Router cache so a
    stale Router (with the old policy) is never silently reused."""
    set_router_config(RouterConfig(use=True, retry_policy={"RateLimitErrorRetries": 3}))
    r1 = _single_deployment_router(_M)
    assert _single_deployment_router(_M) is r1, "same config must hit the cache"

    set_router_config(RouterConfig(use=True, retry_policy={"RateLimitErrorRetries": 7}))
    r2 = _single_deployment_router(_M)
    assert r2 is not r1, (
        "a changed retry_policy must rebuild the Router, not reuse the stale one"
    )
    assert r2.retry_policy is not None
    assert r2.retry_policy.RateLimitErrorRetries == 7


# ── (4) _build_router_config parses retry_policy from raw yaml dict ──────────

def test_build_router_config_parses_retry_policy() -> None:
    """Tier 2: _build_router_config correctly parses the retry_policy mapping from
    a raw reyn.yaml dict into the RouterConfig field."""
    cfg = _build_router_config({
        "use": True,
        "retry_policy": {
            "RateLimitErrorRetries": 5,
            "TimeoutErrorRetries": 2,
        },
    })
    assert cfg.retry_policy == {"RateLimitErrorRetries": 5, "TimeoutErrorRetries": 2}


def test_build_router_config_absent_retry_policy_is_none() -> None:
    """Tier 2: absent retry_policy in raw yaml → RouterConfig.retry_policy is None
    (not an empty dict) — so the Router builder's 'if rcfg.retry_policy' gate is
    falsy and no RetryPolicy is constructed."""
    cfg = _build_router_config({"use": True})
    assert cfg.retry_policy is None
