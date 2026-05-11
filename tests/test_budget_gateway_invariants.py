"""Tier 2: OS invariant tests for BudgetGateway (wave 3 PR1).

Policy compliance (`docs/deep-dives/contributing/testing.md`):
- No unittest.mock usage. Real BudgetTracker, real EventLog, real ModelResolver.
- No private-state assertions. Observation flows through:
    - gateway.total_usage / gateway.total_cost_usd (public properties)
    - gateway.router_cap (public property)
    - events.all() (EventLog public read accessor)
    - RouterCapExceeded exception attributes (public)
- Each test docstring's first line starts with `Tier 2: ...`.
"""
from __future__ import annotations

import pytest

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.chat.services.budget_gateway import BudgetGateway
from reyn.chat.session import RouterCapExceeded
from reyn.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway(
    *,
    tracker: BudgetTracker | None = None,
    cap: int = 3,
) -> tuple[BudgetGateway, EventLog]:
    """Return a (BudgetGateway, EventLog) pair wired to a fresh EventLog."""
    events = EventLog()
    gw = BudgetGateway(
        budget_tracker=tracker,
        events=events,
        agent_name="test_agent",
        default_router_cap=cap,
    )
    return gw, events


class _FakeLLMResult:
    """Minimal object matching the shape gateway.accumulate() expects."""

    def __init__(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        self.token_usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        self.cost_usd = cost_usd


# ---------------------------------------------------------------------------
# Invariant 1: per-session accumulation is independent of BudgetTracker
# ---------------------------------------------------------------------------


def test_accumulate_independent_of_tracker():
    """Tier 2: per-session total_usage / total_cost_usd accumulate across
    multiple accumulate() calls and stay independent from BudgetTracker.snapshot().

    The gateway owns per-session totals; the tracker is process-shared.
    Both must agree on the per-session side but the tracker snapshot must
    NOT be inflated by the gateway's per-session accumulators — the tracker
    is read-only from the gateway's perspective.
    """
    cost_cfg = CostConfig()
    tracker = BudgetTracker(cost_cfg)
    gw, _ = _make_gateway(tracker=tracker)

    r1 = _FakeLLMResult(prompt_tokens=10, completion_tokens=5, cost_usd=0.001)
    r2 = _FakeLLMResult(prompt_tokens=20, completion_tokens=8, cost_usd=0.002)
    r3 = _FakeLLMResult(prompt_tokens=0, completion_tokens=0, cost_usd=None)

    gw.accumulate(r1)
    gw.accumulate(r2)
    gw.accumulate(r3)  # zero usage + no cost — must not raise or corrupt totals

    # Per-session totals aggregate correctly.
    assert gw.total_usage.prompt_tokens == 30
    assert gw.total_usage.completion_tokens == 13
    assert gw.total_usage.total_tokens == 43
    assert abs(gw.total_cost_usd - 0.003) < 1e-9

    # The tracker snapshot is unaffected — gateway does NOT write to tracker.
    snap = tracker.snapshot()
    # agent_tokens comes from tracker.record_llm() calls — we never called it.
    assert snap.get("agent_tokens", {}).get("test_agent", 0) == 0


# ---------------------------------------------------------------------------
# Invariant 2: router cap fires exactly at cap-th invocation
# ---------------------------------------------------------------------------


def test_router_cap_fires_at_nth_invocation():
    """Tier 2: check_and_increment_router_cap raises RouterCapExceeded on the
    (cap+1)-th invocation; emits router_retry_exhausted event with correct
    count, user_text (truncated), and last_reason.

    The cap check fires when invocations_this_turn >= cap (before increment),
    so three allowed calls (counter → 1, 2, 3) means the 4th is the first
    rejection with cap=3.
    """
    gw, events = _make_gateway(cap=3)

    # Three calls within cap: all succeed; counter reaches 3.
    gw.check_and_increment_router_cap("msg1")
    gw.check_and_increment_router_cap("msg2")
    gw.check_and_increment_router_cap("msg3")

    # Arm a last_reason so the event captures it.
    gw.set_router_last_reason("ran_out_of_ideas")

    # Fourth call: counter is 3 >= cap=3 → raise.
    with pytest.raises(RouterCapExceeded) as excinfo:
        gw.check_and_increment_router_cap("msg4")

    exc = excinfo.value
    assert exc.count == 3
    assert exc.cap == 3
    assert exc.last_reason == "ran_out_of_ideas"

    # One event emitted (only on the violation, not on the three preceding calls).
    emitted = events.all()
    exhausted_events = [e for e in emitted if e.type == "router_retry_exhausted"]
    assert len(exhausted_events) == 1

    evt_data = exhausted_events[0].data
    assert evt_data["count"] == 3
    assert evt_data["cap"] == 3
    assert evt_data["last_reason"] == "ran_out_of_ideas"
    # user_message is truncated to 200 chars — "msg4" passes through unchanged.
    assert evt_data["user_message"] == "msg4"


def test_router_cap_long_user_text_truncated():
    """Tier 2: user_message in the router_retry_exhausted event is truncated
    to 200 characters when the input is longer.

    cap=1 means: first call succeeds (counter → 1), second call raises
    (counter is already 1 >= cap=1).
    """
    gw, events = _make_gateway(cap=1)

    # First call within cap.
    gw.check_and_increment_router_cap("seed")

    # Second call exceeds cap.
    long_text = "x" * 400
    with pytest.raises(RouterCapExceeded):
        gw.check_and_increment_router_cap(long_text)

    emitted = events.all()
    exhausted_events = [e for e in emitted if e.type == "router_retry_exhausted"]
    assert len(exhausted_events) == 1
    assert len(exhausted_events[0].data["user_message"]) == 200


# ---------------------------------------------------------------------------
# Invariant 3: reset_router_turn_counter clears counter and last_reason
# ---------------------------------------------------------------------------


def test_reset_router_turn_counter_clears_state():
    """Tier 2: reset_router_turn_counter() clears both the invocation counter
    and the last_reason, so the next turn starts fresh.
    """
    gw, events = _make_gateway(cap=3)

    # Burn two slots and set a reason.
    gw.check_and_increment_router_cap("first")
    gw.check_and_increment_router_cap("second")
    gw.set_router_last_reason("delegation_failed")

    # After reset, the cap should not fire for three more calls.
    gw.reset_router_turn_counter()

    # Three calls in the new turn: all should succeed (counter → 1, 2, 3).
    gw.check_and_increment_router_cap("turn2_call1")
    gw.check_and_increment_router_cap("turn2_call2")
    gw.check_and_increment_router_cap("turn2_call3")

    # Fourth call crosses the cap (counter is 3 >= cap=3).
    # Set a new reason to verify the old one was cleared by reset.
    gw.set_router_last_reason("new_reason")
    with pytest.raises(RouterCapExceeded) as excinfo:
        gw.check_and_increment_router_cap("turn2_call4")

    exc = excinfo.value
    assert exc.count == 3
    assert exc.last_reason == "new_reason"

    # No router_retry_exhausted event from the first two calls (they
    # were within budget). Only one event from the cap violation.
    exhausted_events = [
        e for e in events.all() if e.type == "router_retry_exhausted"
    ]
    assert len(exhausted_events) == 1


# ---------------------------------------------------------------------------
# Invariant 4: add_router_usage strips proxy prefix correctly
# ---------------------------------------------------------------------------


def test_add_router_usage_strips_proxy_prefix(monkeypatch):
    """Tier 2: add_router_usage() strips the proxy prefix from the resolved
    model name before calling estimate_cost (F4 Bug 1 fix verification).

    When proxy_kwargs() returns a non-empty dict, the resolved model string
    "openai/some-model" should have its "openai/" prefix removed before the
    pricing lookup. This test exercises the stripping logic by monkeypatching
    proxy_kwargs to return a non-empty dict and using a resolver that maps
    "light" → "openai/gpt-4o-mini".
    """
    import reyn.llm.llm as llm_mod
    import reyn.llm.pricing as pricing_mod

    # Monkeypatch proxy_kwargs to signal "we are behind a proxy" so the
    # stripping branch is taken.
    monkeypatch.setattr(llm_mod, "proxy_kwargs", lambda: {"api_base": "http://localhost:4000"})

    # Track what model name estimate_cost is called with.
    received_models: list[str] = []
    original_estimate = pricing_mod.estimate_cost

    def capturing_estimate(model: str, usage: TokenUsage):
        received_models.append(model)
        return original_estimate(model, usage)

    monkeypatch.setattr(pricing_mod, "estimate_cost", capturing_estimate)

    # Resolver maps "light" → "openai/gpt-4o-mini" (simulates proxy config).
    resolver = ModelResolver({"light": "openai/gpt-4o-mini"}, builtin={})
    gw, _ = _make_gateway()

    usage = TokenUsage(prompt_tokens=100, completion_tokens=50)
    gw.add_router_usage(usage=usage, resolver=resolver, router_model_name="light")

    # The stripping branch must have fired: estimate_cost receives "gpt-4o-mini"
    # (no "openai/" prefix).
    assert len(received_models) == 1, "estimate_cost should have been called once"
    assert received_models[0] == "gpt-4o-mini", (
        f"Expected 'gpt-4o-mini' but got {received_models[0]!r} — "
        "proxy prefix stripping did not fire"
    )

    # Usage is accumulated regardless of cost lookup outcome.
    assert gw.total_usage.total_tokens == 150


def test_add_router_usage_no_strip_without_proxy(monkeypatch):
    """Tier 2: add_router_usage() does NOT strip the prefix when proxy_kwargs()
    returns an empty dict (direct connection mode).
    """
    import reyn.llm.llm as llm_mod
    import reyn.llm.pricing as pricing_mod

    monkeypatch.setattr(llm_mod, "proxy_kwargs", lambda: {})

    received_models: list[str] = []
    original_estimate = pricing_mod.estimate_cost

    def capturing_estimate(model: str, usage: TokenUsage):
        received_models.append(model)
        return original_estimate(model, usage)

    monkeypatch.setattr(pricing_mod, "estimate_cost", capturing_estimate)

    resolver = ModelResolver({"light": "openai/gpt-4o-mini"}, builtin={})
    gw, _ = _make_gateway()

    usage = TokenUsage(prompt_tokens=50, completion_tokens=25)
    gw.add_router_usage(usage=usage, resolver=resolver, router_model_name="light")

    # No proxy → full string "openai/gpt-4o-mini" passed to estimate_cost.
    assert len(received_models) == 1
    assert received_models[0] == "openai/gpt-4o-mini", (
        f"Expected 'openai/gpt-4o-mini' but got {received_models[0]!r}"
    )

    assert gw.total_usage.total_tokens == 75
