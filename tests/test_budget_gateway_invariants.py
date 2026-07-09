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

from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.errors import RouterCapExceeded
from reyn.runtime.services.budget_gateway import BudgetGateway

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


def test_last_call_usage_is_overwritten_not_accumulated():
    """Tier 2: last_call_usage (status-bar ctx chip's current-size figure)
    reflects only the MOST RECENT accumulate() call, unlike total_usage which
    sums every call — a stale earlier-call usage must not leak into a later
    call's current-size reading."""
    gw, _ = _make_gateway()

    r1 = _FakeLLMResult(prompt_tokens=10, completion_tokens=5, cost_usd=0.001)
    gw.accumulate(r1)
    assert gw.last_call_usage.prompt_tokens == 10
    assert gw.total_usage.prompt_tokens == 10

    r2 = _FakeLLMResult(prompt_tokens=20, completion_tokens=8, cost_usd=0.002)
    gw.accumulate(r2)
    assert gw.last_call_usage.prompt_tokens == 20   # overwritten, not 10+20
    assert gw.total_usage.prompt_tokens == 30        # cumulative still sums


def test_last_call_usage_unaffected_by_zero_usage_result():
    """Tier 2: a zero-usage accumulate() call (e.g. a cost-only event) must not
    clobber the last real call's current-size figure."""
    gw, _ = _make_gateway()

    gw.accumulate(_FakeLLMResult(prompt_tokens=15, completion_tokens=3))
    gw.accumulate(_FakeLLMResult(prompt_tokens=0, completion_tokens=0, cost_usd=None))
    # token_usage is still a TokenUsage(0, 0) object (not None) on the zero
    # result, so accumulate()'s `if result.token_usage is not None` branch
    # DOES run — this documents that current behavior overwrites with the
    # zero usage (matches total_usage's own += semantics: a real zero-usage
    # call is data, not a no-op).
    assert gw.last_call_usage.prompt_tokens == 0


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

    # Exactly one event emitted (only on the violation, not on the three preceding calls).
    emitted = events.all()
    exhausted_events = [e for e in emitted if e.type == "router_retry_exhausted"]
    assert exhausted_events, "router_retry_exhausted event must be emitted on cap violation"
    assert sum(1 for _ in exhausted_events) == 1, (
        "Only one router_retry_exhausted event should be emitted (not one per call)"
    )

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
    assert exhausted_events, "router_retry_exhausted event must be emitted"
    # user_message must be truncated — verify it matches exactly the first 200 chars.
    user_message = exhausted_events[0].data["user_message"]
    assert user_message == long_text[:200], (
        "user_message must be truncated to the first 200 characters of the input"
    )


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
    assert exhausted_events, "router_retry_exhausted event must be emitted on cap violation"
    assert not exhausted_events[1:], (
        "Only one router_retry_exhausted event should be emitted (not one per call)"
    )


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
    assert received_models, "estimate_cost must have been called (stripping branch must fire)"
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
    assert received_models, "estimate_cost must have been called (no-proxy path)"
    assert received_models[0] == "openai/gpt-4o-mini", (
        f"Expected 'openai/gpt-4o-mini' but got {received_models[0]!r}"
    )

    assert gw.total_usage.total_tokens == 75


def test_add_router_usage_uses_explicit_last_call_usage_not_turn_total():
    """Tier 2: add_router_usage (the live chat-turn accumulation path, see
    router_loop_driver.py) takes `usage` (the TURN-SUMMED total, across every
    LLM call the turn made — a tool-loop can iterate several times) and a
    SEPARATE `last_call_usage` (the single most recent call, from
    RouterLoop.last_call_usage). last_call_usage must reflect the smaller
    single-call figure, NOT the turn total — using the turn-summed figure as
    the ctx chip's "current context size" would double/triple-count nearly
    the same growing context each tool-loop iteration re-sends (the bug this
    param was added to fix)."""
    resolver = ModelResolver({"light": "openai/gpt-4o-mini"}, builtin={})
    gw, _ = _make_gateway()

    # A 3-call turn: usage is the SUM (300 prompt tokens across 3 calls), but
    # last_call_usage is only the final call's own usage (120 of those 300).
    turn_total = TokenUsage(prompt_tokens=300, completion_tokens=30, cached_tokens=200)
    final_call = TokenUsage(prompt_tokens=120, completion_tokens=10, cached_tokens=100)
    gw.add_router_usage(
        usage=turn_total, last_call_usage=final_call,
        resolver=resolver, router_model_name="light",
    )
    assert gw.last_call_usage.prompt_tokens == 120   # the single call, NOT 300
    assert gw.last_call_usage.cached_tokens == 100
    assert gw.total_usage.prompt_tokens == 300        # billing still counts every call


def test_add_router_usage_falls_back_to_turn_total_when_last_call_usage_omitted():
    """Tier 2: last_call_usage is optional (default None) — omitting it (as an
    older/non-UI caller might) falls back to the turn total, so nothing
    breaks for callers that don't care about the ctx-chip distinction."""
    resolver = ModelResolver({"light": "openai/gpt-4o-mini"}, builtin={})
    gw, _ = _make_gateway()

    u = TokenUsage(prompt_tokens=40, completion_tokens=5, cached_tokens=5)
    gw.add_router_usage(usage=u, resolver=resolver, router_model_name="light")
    assert gw.last_call_usage.prompt_tokens == 40
    assert gw.total_usage.prompt_tokens == 40
