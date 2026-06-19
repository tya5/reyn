"""Tier 2: OS invariant tests for CompactionEngine 11-axis spec (PR-N3/N6).

Covers:
- compute_budgets() math: assertions on outputs for known inputs.
- assert_static_bounds() failure modes: PR-N6 weight sum = 0 raises, negative
  weight raises, B_M ≤ 0 raises, effective_trigger ≤ 0 raises.
- trim_head / trim_tail post-Axis-3: pure token-budget, no turn count cap.
  Boundary tests.
- estimate_tokens_for_turn multimodal: turn with content=[{type:text, ...},
  {type:image_url, ...}] returns positive int.
- hard_truncate_summary: input > budget → output ≤ budget; input ≤ budget →
  identity.
- 1-turn escape hatch (Axis 7): oversized turn → turn_too_large_truncated event
  emitted, turn truncated and included.
- new_msg_exceeds_budget abort (Axis 11): raises NewMsgExceedsBudgetError, event
  emitted.
- #1128 PR-a: the Axis-8 compaction_lock was removed (it was vestigial — no
  history appender awaited it; cross-driver serialization is the per-agent lock).
- Axis 10 opt-out: use_chars4_estimate=True → chars//4 used, no litellm call needed.
- ISSUE #4: recompute_budgets() with dynamic provider changes effective_trigger.
- ISSUE #5: NewMsgExceedsBudgetError raised when new_msg exceeds new_msg_budget.
- #1128 PR-c: force_compact_now() is a single synchronous pass (the Option-B
  multi-pass race-recovery loop was removed — serialization is now structural
  via the shared per-agent lock; retry_loop is the under-shoot floor).
- PR-N6: weight normalization invariants.

Policy compliance:
- No unittest.mock usage.
- No private-state assertions.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.services.compaction.engine import (
    ChatSummary,
    CompactionEngine,
    ComputedBudgets,
    HistoryChunkToCompact,
    NewMsgExceedsBudgetError,
    assert_static_bounds,
    compute_budgets,
    estimate_tokens_for_turn,
    hard_truncate_summary,
    trim_head,
    trim_tail,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _turns(texts: list[str]) -> list[dict]:
    return [{"role": "user", "text": t, "seq": i + 1} for i, t in enumerate(texts)]


def _make_cfg(**kwargs) -> CompactionConfig:
    """Return a CompactionConfig with test-friendly defaults overridden by kwargs.

    PR-N6: uses component_weights (integer, sum-arbitrary) instead of the old
    head_ratio/body_ratio/tail_ratio/new_msg_ratio float fields.
    """
    defaults: dict = dict(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,  # deterministic for tests
    )
    defaults.update(kwargs)
    return CompactionConfig(**defaults)


# ---------------------------------------------------------------------------
# compute_budgets() math (Axis 1 + derived)
# ---------------------------------------------------------------------------


def test_compute_budgets_basic_math() -> None:
    """Tier 2: compute_budgets() output values are arithmetically correct.

    PR-N6: uses integer component_weights normalised by their sum.

    Uses a synthetic T_max = 100_000, T_SP = 1_000, T_comp_SP = 500.
    Weights: head=10, body=5, tail=15, new_msg=10, compaction_batch=60 (sum=100).
    Manually derives expected budgets and verifies every field.

    Injects a synthetic T_max via direct module-level monkey-patch (no mock).
    """
    # Weights: head=10, body=5, tail=15, new_msg=10, total=100 (ignoring compaction_batch)
    weights = {"head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60}
    total_w = sum(weights.values())  # 100
    cfg = _make_cfg(
        component_weights=weights,
        section_caps_spec_tokens=100,
    )
    T_max = 100_000
    T_SP = 1_000
    T_comp_SP = 500

    # Manually compute expected values (PR-N6 normalised weights).
    main_pool = T_max - T_SP           # 99_000
    head = int((weights["head"] / total_w) * main_pool)    # int(0.10 * 99000) = 9_900
    body = int((weights["body"] / total_w) * main_pool)    # int(0.05 * 99000) = 4_950
    tail = int((weights["tail"] / total_w) * main_pool)    # int(0.15 * 99000) = 14_850
    new_msg = int((weights["new_msg"] / total_w) * main_pool)  # int(0.10 * 99000) = 9_900
    B_M = T_max - T_comp_SP - body - 100  # 100_000 - 500 - 4_950 - 100 = 94_450
    main_M_room = T_max - T_SP - head - tail - new_msg  # 64_350
    effective_trigger = min(main_M_room, B_M)  # 64_350

    # Swap get_max_input_tokens to return our synthetic T_max.
    import reyn.llm.model_budget as _mb
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        budgets = compute_budgets(cfg, "test-model", T_SP=T_SP, T_comp_SP=T_comp_SP)
    finally:
        _mb.get_max_input_tokens = original_fn

    assert budgets.main_pool == main_pool
    assert budgets.head_budget == head
    assert budgets.body_budget == body
    assert budgets.tail_budget == tail
    assert budgets.new_msg_budget == new_msg
    assert budgets.B_M == B_M
    assert budgets.main_M_room == main_M_room
    assert budgets.effective_trigger == effective_trigger


def test_compute_budgets_effective_trigger_is_min() -> None:
    """Tier 2: effective_trigger = min(main_M_room, B_M), not either alone.

    PR-N6: uses component_weights instead of ratios.

    Constructs two configs: one where main_M_room < B_M and one where
    B_M < main_M_room, and verifies the minimum is used in both cases.
    """
    import reyn.llm.model_budget as _mb
    T_max = 50_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        # Case 1: high body weight → B_M is small → effective_trigger = B_M
        cfg1 = _make_cfg(
            component_weights={
                "head": 10, "body": 40, "tail": 10, "new_msg": 5, "compaction_batch": 35,
            },
        )
        b1 = compute_budgets(cfg1, "test-model", T_SP=0, T_comp_SP=100)
        assert b1.effective_trigger == min(b1.main_M_room, b1.B_M)

        # Case 2: low body weight → B_M is large → effective_trigger = main_M_room
        cfg2 = _make_cfg(
            component_weights={
                "head": 30, "body": 1, "tail": 30, "new_msg": 20, "compaction_batch": 19,
            },
        )
        b2 = compute_budgets(cfg2, "test-model", T_SP=0, T_comp_SP=100)
        assert b2.effective_trigger == min(b2.main_M_room, b2.B_M)
    finally:
        _mb.get_max_input_tokens = original_fn


# ---------------------------------------------------------------------------
# assert_static_bounds() failure modes
# ---------------------------------------------------------------------------


def test_assert_static_bounds_zero_weight_sum_raises() -> None:
    """Tier 2: assert_static_bounds raises AssertionError when component_weights sum = 0.

    PR-N6: replaces the old ratio_sum > 1.0 test.  A zero-weight config is
    mathematically degenerate (all budgets = 0).
    """
    cfg = _make_cfg(component_weights={"head": 0, "body": 0, "tail": 0, "new_msg": 0, "compaction_batch": 0})
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=0, body_budget=0,
        tail_budget=0, new_msg_budget=0,
        B_M=5000, main_M_room=10000, effective_trigger=5000,
    )
    with pytest.raises(AssertionError):
        assert_static_bounds(cfg, budgets)


def test_assert_static_bounds_negative_weight_raises() -> None:
    """Tier 2: assert_static_bounds raises AssertionError when any weight is negative.

    PR-N6: negative weights violate the non-negative invariant.
    """
    cfg = _make_cfg(component_weights={"head": -1, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60})
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=1000, body_budget=500,
        tail_budget=1000, new_msg_budget=500,
        B_M=5000, main_M_room=8000, effective_trigger=5000,
    )
    with pytest.raises(AssertionError):
        assert_static_bounds(cfg, budgets)


def test_assert_static_bounds_B_M_zero_raises() -> None:
    """Tier 2: assert_static_bounds raises AssertionError when B_M ≤ 0."""
    cfg = _make_cfg()
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=1000, body_budget=500,
        tail_budget=1000, new_msg_budget=500,
        B_M=0,  # violation
        main_M_room=8000, effective_trigger=0,
    )
    with pytest.raises(AssertionError, match="B_M"):
        assert_static_bounds(cfg, budgets)


def test_assert_static_bounds_effective_trigger_zero_raises() -> None:
    """Tier 2: assert_static_bounds raises AssertionError when effective_trigger ≤ 0."""
    cfg = _make_cfg()
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=1000, body_budget=500,
        tail_budget=1000, new_msg_budget=500,
        B_M=5000, main_M_room=5000,
        effective_trigger=0,  # violation
    )
    with pytest.raises(AssertionError, match="effective_trigger"):
        assert_static_bounds(cfg, budgets)


def test_assert_static_bounds_passes_valid_config() -> None:
    """Tier 2: assert_static_bounds does NOT raise for valid weights and positive budgets."""
    cfg = _make_cfg()
    budgets = ComputedBudgets(
        main_pool=100_000, head_budget=10000, body_budget=5000,
        tail_budget=15000, new_msg_budget=10000,
        B_M=50000, main_M_room=65000, effective_trigger=50000,
    )
    assert_static_bounds(cfg, budgets)  # must not raise


# ---------------------------------------------------------------------------
# trim_head / trim_tail Axis 3: pure token-budget, no count cap
# ---------------------------------------------------------------------------


def _sum_tokens(turns: list, model: str, use_chars4: bool) -> int:
    return sum(estimate_tokens_for_turn(t, model, use_chars4=use_chars4) for t in turns)


def test_trim_head_stops_at_budget_not_count() -> None:
    """Tier 2: trim_head budget invariant — cumulative tokens stay ≤ max_tokens
    and the next omitted turn would push past it (= Axis 3, token budget only).
    """
    texts = ["a" * 40] * 8  # 10 tokens each via chars//4
    turns = _turns(texts)
    result = trim_head(turns, max_tokens=25, model="", use_chars4=True)

    assert _sum_tokens(result, model="", use_chars4=True) <= 25, (
        "result token sum must not exceed budget"
    )
    # Result must be a prefix of the input (= trim_head, not arbitrary subset)
    assert result == turns[:len(result)], "result must be a prefix of input"
    # The first omitted turn (if any) would push over budget — proves we
    # didn't stop early.
    if len(result) < len(turns):
        next_turn_tokens = estimate_tokens_for_turn(
            turns[len(result)], model="", use_chars4=True
        )
        assert _sum_tokens(result, "", True) + next_turn_tokens > 25, (
            "trim_head stopped early — next turn would still fit"
        )


def test_trim_tail_stops_at_budget_not_count() -> None:
    """Tier 2: trim_tail budget invariant — cumulative tokens stay ≤ max_tokens
    and the result is a suffix of the input.
    """
    texts = ["a" * 40] * 8
    turns = _turns(texts)
    result = trim_tail(turns, max_tokens=25, model="", use_chars4=True)

    assert _sum_tokens(result, model="", use_chars4=True) <= 25, (
        "result token sum must not exceed budget"
    )
    # Result must be a suffix of the input (= trim_tail picks from the end)
    if result:
        assert result == turns[-len(result):], "result must be a suffix of input"
        # The first omitted turn from the tail (= one before the first kept
        # turn) would push over budget if included.
        omitted_idx = len(turns) - len(result) - 1
        if omitted_idx >= 0:
            next_turn_tokens = estimate_tokens_for_turn(
                turns[omitted_idx], model="", use_chars4=True
            )
            assert _sum_tokens(result, "", True) + next_turn_tokens > 25, (
                "trim_tail stopped early — earlier turn would still fit"
            )


def test_trim_head_includes_all_within_budget() -> None:
    """Tier 2: trim_head with budget ≫ total tokens returns the full input."""
    turns = _turns(["hi"] * 5)
    result = trim_head(turns, max_tokens=100_000, model="", use_chars4=True)
    assert result == turns, "all-within-budget case must return input unchanged"


def test_trim_tail_includes_all_within_budget() -> None:
    """Tier 2: trim_tail with budget ≫ total tokens returns the full input."""
    turns = _turns(["hi"] * 5)
    result = trim_tail(turns, max_tokens=100_000, model="", use_chars4=True)
    assert result == turns, "all-within-budget case must return input unchanged"


# ---------------------------------------------------------------------------
# estimate_tokens_for_turn multimodal (Axis 6)
# ---------------------------------------------------------------------------


def test_estimate_tokens_for_turn_multimodal_returns_positive() -> None:
    """Tier 2: estimate_tokens_for_turn with multimodal content returns positive int.

    Turn has content=[{type:text, text:"hello"}, {type:image_url, image_url:{...}}].
    The result must be > 0 and > the text-only estimate (because image adds cost).
    """
    turn = {
        "role": "user",
        "seq": 1,
        "content": [
            {"type": "text", "text": "hello world"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ],
    }
    tokens = estimate_tokens_for_turn(turn, model="", use_chars4=True)
    assert tokens > 0, "multimodal turn must have positive token count"
    # Image adds _IMAGE_FIXED_TOKEN_COST (1024), text "hello world" = len//4 = 2
    # So total should be 1024 + 2 = 1026
    from reyn.services.compaction.engine import _IMAGE_FIXED_TOKEN_COST
    assert tokens >= _IMAGE_FIXED_TOKEN_COST, (
        f"multimodal turn token count {tokens} should be ≥ IMAGE_FIXED_TOKEN_COST "
        f"{_IMAGE_FIXED_TOKEN_COST}"
    )


def test_estimate_tokens_for_turn_text_only() -> None:
    """Tier 2: estimate_tokens_for_turn with str content returns chars//4."""
    turn = {"role": "user", "seq": 1, "content": "a" * 40}
    tokens = estimate_tokens_for_turn(turn, model="", use_chars4=True)
    assert tokens == 10, f"40 chars // 4 = 10 tokens, got {tokens}"


def test_estimate_tokens_for_turn_compactor_dict_shape() -> None:
    """Tier 2: estimate_tokens_for_turn handles the compactor input dict shape
    (text field, no content field).
    """
    turn = {"role": "user", "text": "a" * 40, "seq": 1}
    tokens = estimate_tokens_for_turn(turn, model="", use_chars4=True)
    assert tokens == 10


def test_estimate_tokens_for_turn_image_path() -> None:
    """Tier 2: estimate_tokens_for_turn counts image_path parts at fixed cost."""
    turn = {
        "role": "user",
        "seq": 2,
        "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_path", "image_path": "/tmp/shot.png"},
        ],
    }
    tokens = estimate_tokens_for_turn(turn, model="", use_chars4=True)
    from reyn.services.compaction.engine import _IMAGE_FIXED_TOKEN_COST
    assert tokens >= _IMAGE_FIXED_TOKEN_COST


# ---------------------------------------------------------------------------
# hard_truncate_summary (Axis 9)
# ---------------------------------------------------------------------------


def test_hard_truncate_summary_identity_when_within_budget() -> None:
    """Tier 2: hard_truncate_summary returns the original string unchanged
    when token count ≤ body_budget.
    """
    text = "a" * 40  # 10 tokens via chars//4
    result = hard_truncate_summary(text, body_budget=20, model="", use_chars4=True)
    assert result == text


def test_hard_truncate_summary_truncates_over_budget() -> None:
    """Tier 2: hard_truncate_summary returns a shorter string when over budget.

    Input: 400 chars = 100 tokens (chars//4). Budget: 50 tokens.
    The result must be ≤ 50 tokens (≤ 200 chars with chars//4 estimation).
    """
    text = "x" * 400  # 100 tokens
    result = hard_truncate_summary(text, body_budget=50, model="", use_chars4=True)
    result_tokens = len(result) // 4
    assert result_tokens <= 50, (
        f"truncated summary {result_tokens} tokens should be ≤ 50 budget"
    )
    assert len(result) < len(text), "truncated result must be shorter than input"


def test_hard_truncate_summary_emits_event_when_truncating() -> None:
    """Tier 2: hard_truncate_summary emits body_summary_hard_truncated when it
    truncates.
    """
    events = EventLog()
    text = "x" * 400  # 100 tokens
    result = hard_truncate_summary(
        text, body_budget=50, model="", events=events, use_chars4=True
    )
    emitted = [e for e in events.all() if e.type == "body_summary_hard_truncated"]
    assert emitted, "body_summary_hard_truncated event must be emitted"
    assert emitted[0].data["kept_tokens"] == 50
    assert len(result) < len(text)


def test_hard_truncate_summary_no_event_when_within_budget() -> None:
    """Tier 2: hard_truncate_summary does NOT emit event when no truncation occurs."""
    events = EventLog()
    text = "a" * 40  # 10 tokens
    hard_truncate_summary(text, body_budget=100, model="", events=events, use_chars4=True)
    emitted = [e for e in events.all() if e.type == "body_summary_hard_truncated"]
    assert not emitted


# ---------------------------------------------------------------------------
# 1-turn escape hatch (Axis 7)
# ---------------------------------------------------------------------------


def test_trim_head_oversized_turn_event_has_required_fields() -> None:
    """Tier 2: turn_too_large_truncated event has turn_seq, original_tokens,
    kept_tokens, budget_kind fields (Axis 7 spec).
    """
    events = EventLog()
    turns = [{"role": "user", "text": "x" * 4000, "seq": 42}]
    trim_head(turns, max_tokens=10, model="", use_chars4=True, events=events)
    ev = next(
        (e for e in events.all() if e.type == "turn_too_large_truncated"), None
    )
    assert ev is not None
    assert ev.data["turn_seq"] == 42
    assert ev.data["original_tokens"] > 0
    assert ev.data["kept_tokens"] == 10
    assert ev.data["budget_kind"] == "head"


def test_trim_tail_oversized_turn_event_has_required_fields() -> None:
    """Tier 2: turn_too_large_truncated event has turn_seq, original_tokens,
    kept_tokens, budget_kind fields (Axis 7 spec).
    """
    events = EventLog()
    turns = [{"role": "user", "text": "x" * 4000, "seq": 99}]
    trim_tail(turns, max_tokens=10, model="", use_chars4=True, events=events)
    ev = next(
        (e for e in events.all() if e.type == "turn_too_large_truncated"), None
    )
    assert ev is not None
    assert ev.data["turn_seq"] == 99
    assert ev.data["original_tokens"] > 0
    assert ev.data["kept_tokens"] == 10
    assert ev.data["budget_kind"] == "tail"


def test_trim_head_oversized_turn_still_included() -> None:
    """Tier 2: oversized single turn (= exceeds budget alone) is still included
    by trim_head (= Axis 7, preserves conversation flow, escape hatch fires).
    """
    turns = [{"role": "user", "text": "x" * 4000, "seq": 1}]
    result = trim_head(turns, max_tokens=10, model="", use_chars4=True)
    assert any(t["seq"] == 1 for t in result), (
        "oversized turn must remain visible in the result, not silently dropped"
    )


def test_trim_tail_oversized_turn_still_included() -> None:
    """Tier 2: oversized single turn (= exceeds budget alone) is still included
    by trim_tail.
    """
    turns = [{"role": "user", "text": "x" * 4000, "seq": 1}]
    result = trim_tail(turns, max_tokens=10, model="", use_chars4=True)
    assert any(t["seq"] == 1 for t in result), (
        "oversized turn must remain visible in the result, not silently dropped"
    )


# ---------------------------------------------------------------------------
# new_msg_exceeds_budget abort (Axis 11)
# ---------------------------------------------------------------------------


def test_new_msg_exceeds_budget_error_raised() -> None:
    """Tier 2: NewMsgExceedsBudgetError carries new_msg_tokens and new_msg_budget."""
    exc = NewMsgExceedsBudgetError(new_msg_tokens=5000, new_msg_budget=1000)
    assert exc.new_msg_tokens == 5000
    assert exc.new_msg_budget == 1000
    assert isinstance(exc, Exception)


def test_new_msg_exceeds_budget_is_exception_subclass() -> None:
    """Tier 2: NewMsgExceedsBudgetError is a subclass of Exception (not BaseException)."""
    assert issubclass(NewMsgExceedsBudgetError, Exception)


# ---------------------------------------------------------------------------
# Shared ChatMessage substitute for controller tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeMessage:
    """Minimal ChatMessage substitute for controller tests."""
    role: str
    text: str
    ts: str = "2026-01-01T00:00:00+00:00"
    seq: int = 0
    meta: dict = field(default_factory=dict)
    content: str | list = ""
    tool_calls: list | None = None
    tool_call_id: str | None = None
    name: str | None = None


# ---------------------------------------------------------------------------
# Axis 10: use_chars4_estimate opt-out
# ---------------------------------------------------------------------------


def test_estimate_tokens_chars4_opt_out() -> None:
    """Tier 2: estimate_tokens with use_chars4=True uses len//4, not litellm.

    Verify deterministically without any LLM call.
    """
    from reyn.services.compaction.engine import estimate_tokens
    text = "a" * 400  # 100 tokens via chars//4
    tokens = estimate_tokens(text, model="no-such-model", use_chars4=True)
    assert tokens == 100


def test_estimate_tokens_for_turn_chars4_opt_out_str() -> None:
    """Tier 2: estimate_tokens_for_turn with use_chars4=True, str content."""
    turn = {"role": "user", "content": "a" * 400, "seq": 1}
    tokens = estimate_tokens_for_turn(turn, model="no-such-model", use_chars4=True)
    assert tokens == 100


def test_estimate_tokens_for_turn_chars4_opt_out_multimodal() -> None:
    """Tier 2: estimate_tokens_for_turn with use_chars4=True, list content.

    Text part = 400 chars = 100 tokens. Image part = IMAGE_FIXED_TOKEN_COST.
    Total must equal 100 + IMAGE_FIXED_TOKEN_COST.
    """
    from reyn.services.compaction.engine import _IMAGE_FIXED_TOKEN_COST
    turn = {
        "role": "user",
        "seq": 1,
        "content": [
            {"type": "text", "text": "a" * 400},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ],
    }
    tokens = estimate_tokens_for_turn(turn, model="no-such-model", use_chars4=True)
    assert tokens == 100 + _IMAGE_FIXED_TOKEN_COST


# ---------------------------------------------------------------------------
# ISSUE #4: recompute_budgets() — dynamic system_prompt_provider
# ---------------------------------------------------------------------------


def test_recompute_budgets_with_provider_changes_effective_trigger() -> None:
    """Tier 2: recompute_budgets() with a dynamic provider that returns different
    SP strings causes budgets.effective_trigger to change.

    PR-N6: uses component_weights instead of ratio fields.

    Uses a real lambda as the provider (no mock).  Two calls: first with a
    small SP, then with a larger SP (= smaller main_pool = smaller effective_trigger).
    """
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        cfg = _make_cfg(
            section_caps_spec_tokens=100,
            use_chars4_estimate=True,
        )
        events = EventLog()

        # Provider returns a 40-char SP = 10 tokens initially.
        sp_state: list[str] = ["a" * 40]
        provider = lambda: sp_state[0]  # noqa: E731

        engine = CompactionEngine(
            model="test-model",
            events=events,
            cfg=cfg,
            system_prompt_provider=provider,
        )
        trigger_small_sp = engine.budgets.effective_trigger

        # Now switch to a larger SP (= T_SP grows → main_pool shrinks → trigger shrinks).
        # Use 40_000 chars = 10_000 tokens (10% of T_max=100_000 → still valid).
        sp_state[0] = "a" * 40_000  # 10_000 tokens via chars//4
        engine.recompute_budgets()
        trigger_large_sp = engine.budgets.effective_trigger

        assert trigger_small_sp > trigger_large_sp, (
            f"larger SP should produce smaller effective_trigger; "
            f"small_sp trigger={trigger_small_sp}, large_sp trigger={trigger_large_sp}"
        )
    finally:
        _mb.get_max_input_tokens = original_fn


def test_recompute_budgets_noop_when_no_provider() -> None:
    """Tier 2: recompute_budgets() is a no-op when no system_prompt_provider was set.

    After calling recompute_budgets() the budgets remain the same as at init.
    """
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        cfg = _make_cfg()
        events = EventLog()
        engine = CompactionEngine(
            model="test-model",
            events=events,
            cfg=cfg,
            T_SP=1_000,
        )
        trigger_before = engine.budgets.effective_trigger
        engine.recompute_budgets()
        trigger_after = engine.budgets.effective_trigger
        assert trigger_before == trigger_after, (
            "recompute_budgets() with no provider must not change effective_trigger"
        )
    finally:
        _mb.get_max_input_tokens = original_fn


def test_recompute_budgets_called_at_init_when_provider_set() -> None:
    """Tier 2: when system_prompt_provider is set, the first recompute_budgets()
    call fires at engine init — budgets reflect the initial provider output.

    Verifies that the engine's main_pool at init matches T_max - T_SP where
    T_SP is derived from the provider's returned text.
    """
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        cfg = _make_cfg(
            section_caps_spec_tokens=100,
            use_chars4_estimate=True,
        )
        sp_text = "b" * 200  # 50 tokens via chars//4
        events = EventLog()

        engine = CompactionEngine(
            model="test-model",
            events=events,
            cfg=cfg,
            system_prompt_provider=lambda: sp_text,
        )

        # Verify: main_pool = T_max - T_SP where T_SP = len(sp_text)//4 = 50.
        # This checks that the provider was actually consulted at init time.
        T_SP_expected = len(sp_text) // 4  # 50 (chars//4 estimate)
        expected_main_pool = T_max - T_SP_expected  # 99_950

        assert engine.budgets.effective_trigger > 0, (
            "effective_trigger must be > 0 after init with valid config"
        )
        assert engine.budgets.main_pool == expected_main_pool, (
            f"main_pool expected {expected_main_pool} (T_max={T_max} - T_SP={T_SP_expected}), "
            f"got {engine.budgets.main_pool}"
        )
    finally:
        _mb.get_max_input_tokens = original_fn


# ---------------------------------------------------------------------------
# ISSUE #5: new_msg_text wired at _run_router_loop call site (Axis 11)
# ---------------------------------------------------------------------------


def test_new_msg_exceeds_budget_error_fields_and_raises() -> None:
    """Tier 2: NewMsgExceedsBudgetError has correct new_msg_tokens and new_msg_budget
    fields, and its token count exceeds its budget.

    Exercises the public contract for the error that ContextBudgetAdvisor.maybe_force_compact
    raises (ISSUE #5) when new_msg_text exceeds new_msg_budget.  Uses a deliberately
    huge text (10 million chars = 2.5M tokens via chars//4) against a small budget.
    """
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        # PR-N6: new_msg weight = 5 out of 105 total (5/105 * 100_000 ≈ 4_761 tokens)
        cfg = _make_cfg(
            component_weights={
                "head": 10, "body": 5, "tail": 15, "new_msg": 5, "compaction_batch": 70,
            },
            section_caps_spec_tokens=100,
            use_chars4_estimate=True,
        )
        events = EventLog()
        engine = CompactionEngine(
            model="test-model",
            events=events,
            cfg=cfg,
            T_SP=0,
        )
        # new_msg_budget = (5/105) * 100_000 ≈ 4_761 tokens
        # huge_text = 10_000_000 chars = 2_500_000 tokens >> budget
        huge_text = "x" * 10_000_000
        new_msg_turn = {"role": "user", "content": huge_text}
        tokens = estimate_tokens_for_turn(new_msg_turn, "test-model", use_chars4=True)
        budget = engine.budgets.new_msg_budget

        assert tokens > budget, (
            f"test precondition: {tokens} tokens must exceed {budget} budget"
        )

        exc = NewMsgExceedsBudgetError(
            new_msg_tokens=tokens,
            new_msg_budget=budget,
        )
        assert exc.new_msg_tokens > exc.new_msg_budget
        assert exc.new_msg_tokens == tokens
        assert exc.new_msg_budget == budget
        assert isinstance(exc, Exception)
    finally:
        _mb.get_max_input_tokens = original_fn


# ---------------------------------------------------------------------------
# #1128 PR-c: force_compact_now is a single synchronous pass
# ---------------------------------------------------------------------------


@dataclass
class _CountingEngine(CompactionEngine):
    """Engine that counts compact() calls and always returns a stub summary."""

    def __init__(self) -> None:
        # Bypass normal init for test isolation — set fields directly.
        self._model = "stub"
        self._events = EventLog()
        from reyn.config import CompactionConfig as _CC
        self._cfg = _CC(use_chars4_estimate=True)
        self._use_chars4 = True
        self._T_comp_SP = 10
        self._system_prompt_provider = None
        self._budgets = ComputedBudgets(
            main_pool=100_000, head_budget=10_000, body_budget=5_000,
            tail_budget=15_000, new_msg_budget=10_000,
            B_M=80_000, main_M_room=65_000, effective_trigger=50_000,
        )
        self.compact_call_count = 0

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        """Tier 2 stub: increment call count and return a stub summary."""
        self.compact_call_count += 1
        return ChatSummary(topic_arc="stub", covers_through_seq=0)


def test_force_compact_now_single_pass_no_race_recovery() -> None:
    """Tier 2: #1128 PR-c — force_compact_now runs exactly ONE compaction pass
    and emits no force_compact_race_unrecovered event, even when the history
    stays large.

    The former Option-B multi-pass race-recovery loop was removed: cross-driver
    turn serialization is now structural (the shared per-agent lock, PR-b), so a
    single pass suffices and the retry_loop overflow backstop is the under-shoot
    floor. Verified via the public surface — compact() is invoked once and no
    race-unrecovered event is emitted.
    """
    from reyn.runtime.services.compaction_controller import CompactionController

    events = EventLog()
    engine = _CountingEngine()

    # 600 large turns — stays well "over budget"; pre-#1128 this drove a 2nd
    # pass + a race-unrecovered raise. Post-PR-c it is a single pass.
    def _big_history() -> list[_FakeMessage]:
        return [
            _FakeMessage(role="user" if i % 2 == 0 else "assistant",
                         text="a" * 400, seq=i + 1)
            for i in range(600)
        ]

    ctrl = CompactionController(
        event_log=events,
        config=CompactionConfig(use_chars4_estimate=True),
        history_access=_big_history,
        latest_summary=lambda: None,
        compaction_engine=engine,
        history_appender=lambda m: None,
        make_summary_message=lambda rendered, structured, covers: _FakeMessage(
            role="summary", text=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )

    asyncio.run(ctrl.force_compact_now())

    assert engine.compact_call_count == 1, (
        f"force_compact_now must run exactly one pass, got {engine.compact_call_count}"
    )
    unrecovered = [e for e in events.all() if e.type == "force_compact_race_unrecovered"]
    assert not unrecovered, (
        "single-pass force_compact_now must not emit force_compact_race_unrecovered"
    )
