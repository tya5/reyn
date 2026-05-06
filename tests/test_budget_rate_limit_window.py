"""Tier 2: BudgetTracker — per-model rate-limit window invariants.

Covers the rolling 60-second call-count window that limits LLM calls
per model.  All assertions use the public surface: check_pre_llm(),
record_llm(), reset_all(), and snapshot().

Observable-but-not-pinnable note
─────────────────────────────────
The "window resets after 60 seconds have elapsed" invariant (the sliding-
window expiry) CANNOT be pinned from the public API without either:
  • time.monotonic() mock  (forbidden by testing policy)
  • an injectable clock callable  (BudgetTracker has none)
  • a 60-second sleep  (infeasible in CI)

The closest approximation is test_rate_limit_window_resets_via_reset_all,
which verifies the window can be cleared via the public reset_all() API —
the intended operational escape hatch when the window is exhausted.
The temporal-expiry invariant itself is documented as un-pinnable here.
"""
from __future__ import annotations

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.llm.pricing import TokenUsage

# ── helpers ──────────────────────────────────────────────────────────────────


def _tracker(rate_limit: int, model: str = "m") -> BudgetTracker:
    """Return a BudgetTracker with a rate limit of ``rate_limit`` calls/min."""
    cfg = CostConfig(rate_limit_per_minute={model: rate_limit})
    return BudgetTracker(cfg)


def _record(bt: BudgetTracker, model: str = "m", n: int = 1) -> None:
    """Call record_llm n times against the given model."""
    for _ in range(n):
        bt.record_llm(model=model, agent=None, usage=TokenUsage(10, 5))


# ── tests ─────────────────────────────────────────────────────────────────────


def test_rate_limit_within_window_blocks():
    """Tier 2: once the per-model rate cap is reached, the next check is denied.

    Invariant: when the call count within the rolling 60-second window equals
    the configured hard limit, check_pre_llm must return allowed=False with
    hard_dimension="rate_limit".
    """
    model = "fast-model"
    cap = 3
    bt = _tracker(rate_limit=cap, model=model)

    # Fill the window to the cap by recording calls (window entries are added
    # in record_llm, not in check_pre_llm).
    _record(bt, model=model, n=cap)

    # The window is now full; next pre-call check must be refused.
    result = bt.check_pre_llm(model=model, agent=None)

    assert not result.allowed, (
        f"Expected check_pre_llm to refuse after {cap}/{cap} calls; "
        f"got allowed=True"
    )
    assert result.hard_dimension == "rate_limit", (
        f"Expected hard_dimension='rate_limit', got {result.hard_dimension!r}"
    )
    assert result.context.get("current") == cap
    assert result.context.get("hard") == cap


def test_rate_limit_below_cap_is_allowed():
    """Tier 2: calls below the per-model cap are permitted.

    Verifies the happy-path boundary: cap-1 calls must still return
    allowed=True so the OS does not over-refuse.
    """
    model = "slow-model"
    cap = 5
    bt = _tracker(rate_limit=cap, model=model)

    _record(bt, model=model, n=cap - 1)

    result = bt.check_pre_llm(model=model, agent=None)

    assert result.allowed, (
        f"Expected allowed after {cap-1}/{cap} calls; got allowed=False "
        f"(hard_dimension={result.hard_dimension!r})"
    )


def test_rate_limit_window_resets_via_reset_all():
    """Tier 2: reset_all() clears the rate-limit window, restoring full capacity.

    This is the public-API approximation of the temporal-expiry invariant
    (see module docstring for why actual clock-advance testing is not pinnable).
    After reset_all(), the window count returns to 0 and calls are permitted
    again even though the cap was fully consumed before the reset.
    """
    model = "chat-model"
    cap = 2
    bt = _tracker(rate_limit=cap, model=model)

    # Exhaust the cap.
    _record(bt, model=model, n=cap)
    before = bt.check_pre_llm(model=model, agent=None)
    assert not before.allowed, "Pre-condition: cap should be exhausted"

    # Reset the window via the public API.
    bt.reset_all()

    # Window count must be zero in snapshot.
    snap = bt.snapshot()
    rate_window = snap.get("rate_window", {})
    assert rate_window.get(model, 0) == 0, (
        f"rate_window[{model!r}] should be 0 after reset_all(); "
        f"got {rate_window}"
    )

    # And check_pre_llm must now be allowed.
    after = bt.check_pre_llm(model=model, agent=None)
    assert after.allowed, (
        "check_pre_llm should allow calls after reset_all() clears the window"
    )


def test_load_state_does_not_restore_rate_limit_window(tmp_path):
    """Tier 2: the rate-limit window is intentionally excluded from save/load.

    Rationale: window entries older than 60 s are already invalid on any
    restart path. Restoring stale entries would wrongly block calls that
    were made >60 s before the crash.  save_state / load_state MUST NOT
    persist or restore the rate-limit window.
    """
    model = "persist-model"
    cap = 5
    cfg = CostConfig(rate_limit_per_minute={model: cap})
    bt = BudgetTracker(cfg)

    # Exhaust the cap, then save.
    _record(bt, model=model, n=cap)
    state_path = tmp_path / "budget_state.json"
    bt.save_state(state_path)

    # Load into a fresh tracker with the same config.
    bt2 = BudgetTracker(cfg)
    bt2.load_state(state_path)

    # The window must be empty — the loaded state carries no rate entries.
    snap = bt2.snapshot()
    assert snap["rate_window"].get(model, 0) == 0, (
        "rate_limit window must not be restored from state file; "
        f"got {snap['rate_window']}"
    )

    # Accordingly, calls are allowed again immediately after load.
    result = bt2.check_pre_llm(model=model, agent=None)
    assert result.allowed, (
        "After load_state, rate-limit window is empty so calls must be allowed"
    )


def test_rate_limit_warn_threshold_fires_before_hard_cap():
    """Tier 2: the warn threshold is emitted before the hard cap is hit.

    With rate_limit_warn_ratio=0.8 and cap=5, the warn fires when the
    count in the window reaches floor(5 * 0.8) = 4. The BudgetCheck
    returned by check_pre_llm must include "rate_limit" in warn_dimensions
    at that point, while still being allowed.
    """
    model = "warn-model"
    cap = 5
    warn_ratio = 0.8
    warn_threshold = int(cap * warn_ratio)  # 4

    cfg = CostConfig(
        rate_limit_per_minute={model: cap},
        rate_limit_warn_ratio=warn_ratio,
    )
    bt = BudgetTracker(cfg)

    # Record up to the warn threshold so the next check sees current == threshold.
    _record(bt, model=model, n=warn_threshold)

    # check_pre_llm inspects window *before* appending, so current == warn_threshold.
    result = bt.check_pre_llm(model=model, agent=None)

    assert result.allowed, (
        f"check_pre_llm should still allow at warn threshold ({warn_threshold}/{cap})"
    )
    assert "rate_limit" in result.warn_dimensions, (
        f"Expected 'rate_limit' in warn_dimensions at {warn_threshold}/{cap}; "
        f"got {result.warn_dimensions}"
    )
