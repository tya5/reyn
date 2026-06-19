"""Tier 2: OS-invariant tests for #1829 S2 — loop-aware Router cache + cost-fix.

S2 adds (a) a per-running-loop cache for the single-deployment Router (reusing the
#1762 loop-aware-registry pattern — a litellm.Router binds to the loop it first
awaits on, so a process-global cache would trip "bound to a different event loop"
under pytest-asyncio per-test loops), and (b) the cost-mutation fix in
``estimate_cost``: a litellm.model_cost entry with None prices (a price-less
placeholder, e.g. a Router deployment registration) means cost UNKNOWN → (None,
None), not 0.0 (unknown != free).

Policy: no mocks; real Router + real estimate_cost; no private-state/count-pins;
Tier line.
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from reyn.llm.llm import TokenUsage, _single_deployment_router
from reyn.llm.pricing import estimate_cost


@pytest.fixture(autouse=True)
def _isolate_litellm_model_cost():
    """Restore litellm.model_cost in place (Router build registers placeholders;
    same #1762 global-state-isolation discipline as the S1 test)."""
    before = dict(litellm.model_cost)
    yield
    litellm.model_cost.clear()
    litellm.model_cost.update(before)


# (a) loop-aware Router cache ------------------------------------------------------

@pytest.mark.asyncio
async def test_router_cached_within_a_loop() -> None:
    """Tier 2: repeated calls within one running loop return the SAME cached Router."""
    a = _single_deployment_router("openai/gemini-2.5-flash-lite")
    b = _single_deployment_router("openai/gemini-2.5-flash-lite")
    assert a is b, "the single-deployment Router must be cached per running loop"


def test_router_distinct_across_event_loops() -> None:
    """Tier 2: distinct event loops get DISTINCT Routers (no cross-loop binding).

    A cached Router reused across loops would raise "bound to a different event
    loop" (the #1762 class). Two separate asyncio.run() loops must each get their
    own Router without error — the loop-aware cache key guarantees this.
    """
    async def _get() -> int:
        return id(_single_deployment_router("openai/gemini-2.5-flash-lite"))

    id1 = asyncio.run(_get())   # loop 1
    id2 = asyncio.run(_get())   # loop 2 — must not raise, must be a fresh Router
    assert id1 != id2, "each event loop must get its own Router (loop-aware cache)"


# (b) cost-mutation fix (estimate_cost) -------------------------------------------

def test_estimate_cost_none_price_placeholder_returns_none() -> None:
    """Tier 2: a None-price model_cost placeholder → (None, None), not 0.0.

    A litellm.Router deployment registration adds a price-less placeholder entry to
    the global litellm.model_cost; estimate_cost must treat that as cost UNKNOWN
    (None), not free (0.0). This keeps cost-recording correct when the Router goes
    live (#1829 S2 cost-mutation gate).
    """
    litellm.model_cost["openai/gemini-2.5-flash-lite"] = {
        "input_cost_per_token": None, "output_cost_per_token": None,
        "litellm_provider": None,
    }
    cost, snapshot = estimate_cost(
        "openai/gemini-2.5-flash-lite",
        TokenUsage(prompt_tokens=1000, completion_tokens=500),
    )
    assert cost is None and snapshot is None, (
        "an unpriced (None-price) model entry means cost UNKNOWN → (None, None), "
        "not 0.0 — unknown != free"
    )


def test_estimate_cost_real_priced_model_unchanged() -> None:
    """Tier 2: a real-priced model still computes a positive cost (fix non-regression)."""
    cost, snapshot = estimate_cost(
        "gpt-4o-mini",
        TokenUsage(prompt_tokens=1000, completion_tokens=500),
    )
    assert cost is not None and cost > 0, (
        "the None-price guard must NOT affect real-priced models — they still cost"
    )
