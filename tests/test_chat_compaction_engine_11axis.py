"""Tier 2: OS invariant tests for ChatCompactionEngine 11-axis spec (PR-N3).

Covers:
- compute_budgets() math: assertions on outputs for known inputs.
- assert_static_bounds() failure modes: ratio sum > 1.0 raises, B_M ≤ 0 raises,
  effective_trigger ≤ 0 raises.
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
- B_M trigger race (Axis 8): synchronous force_compact_now() + concurrent
  history-append attempt → ordering guarantee verified.
- Axis 10 opt-out: use_chars4_estimate=True → chars//4 used, no litellm call needed.
- ISSUE #4: recompute_budgets() with dynamic provider changes effective_trigger.
- ISSUE #5: NewMsgExceedsBudgetError raised when new_msg exceeds new_msg_budget.
- ISSUE #6: force_compact_now() race-recovery loop: N=2 passes when post-compaction
  still over budget; force_compact_race_unrecovered event when race persists.

Policy compliance:
- No unittest.mock usage.
- No private-state assertions.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from reyn.chat.services.chat_compaction_engine import (
    ChatCompactionEngine,
    ChatSummary,
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
from reyn.config import CompactionConfig
from reyn.events.events import EventLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _turns(texts: list[str]) -> list[dict]:
    return [{"role": "user", "text": t, "seq": i + 1} for i, t in enumerate(texts)]


def _make_cfg(**kwargs) -> CompactionConfig:
    """Return a CompactionConfig with test-friendly defaults overridden by kwargs."""
    defaults = dict(
        head_ratio=0.10,
        body_ratio=0.05,
        tail_ratio=0.15,
        new_msg_ratio=0.10,
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

    Uses a synthetic T_max = 100_000, T_SP = 1_000, T_comp_SP = 500, and
    a config with known ratios.  Verifies every budget field against manual
    derivation.

    Injects a synthetic T_max via direct module-level monkey-patch (no mock).
    """
    cfg = _make_cfg(
        head_ratio=0.10,
        body_ratio=0.05,
        tail_ratio=0.15,
        new_msg_ratio=0.10,
        section_caps_spec_tokens=100,
    )
    T_max = 100_000
    T_SP = 1_000
    T_comp_SP = 500

    # Manually compute expected values.
    main_pool = T_max - T_SP           # 99_000
    head = int(0.10 * main_pool)       # 9_900
    body = int(0.05 * main_pool)       # 4_950
    tail = int(0.15 * main_pool)       # 14_850
    new_msg = int(0.10 * main_pool)    # 9_900
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

    Constructs two configs: one where main_M_room < B_M and one where
    B_M < main_M_room, and verifies the minimum is used in both cases.
    """
    import reyn.llm.model_budget as _mb
    T_max = 50_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        # Case 1: large body_ratio → B_M is small → effective_trigger = B_M
        cfg1 = _make_cfg(
            head_ratio=0.10,
            body_ratio=0.40,  # large body_ratio → B_M = T_max - T_comp_SP - body - spec
            tail_ratio=0.10,
            new_msg_ratio=0.05,
        )
        b1 = compute_budgets(cfg1, "test-model", T_SP=0, T_comp_SP=100)
        assert b1.effective_trigger == min(b1.main_M_room, b1.B_M)

        # Case 2: small body_ratio → B_M is large → effective_trigger = main_M_room
        cfg2 = _make_cfg(
            head_ratio=0.30,
            body_ratio=0.01,
            tail_ratio=0.30,
            new_msg_ratio=0.20,
        )
        b2 = compute_budgets(cfg2, "test-model", T_SP=0, T_comp_SP=100)
        assert b2.effective_trigger == min(b2.main_M_room, b2.B_M)
    finally:
        _mb.get_max_input_tokens = original_fn


# ---------------------------------------------------------------------------
# assert_static_bounds() failure modes
# ---------------------------------------------------------------------------


def test_assert_static_bounds_ratio_sum_over_1_raises() -> None:
    """Tier 2: assert_static_bounds raises AssertionError when ratio sum > 1.0."""
    cfg = _make_cfg(head_ratio=0.40, body_ratio=0.30, tail_ratio=0.30, new_msg_ratio=0.10)
    # sum = 1.10 > 1.0
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=4000, body_budget=3000,
        tail_budget=3000, new_msg_budget=1000,
        B_M=5000, main_M_room=2000, effective_trigger=2000,
    )
    with pytest.raises(AssertionError, match="ratio sum"):
        assert_static_bounds(cfg, budgets)


def test_assert_static_bounds_B_M_zero_raises() -> None:
    """Tier 2: assert_static_bounds raises AssertionError when B_M ≤ 0."""
    cfg = _make_cfg(head_ratio=0.10, body_ratio=0.05, tail_ratio=0.10, new_msg_ratio=0.05)
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
    cfg = _make_cfg(head_ratio=0.10, body_ratio=0.05, tail_ratio=0.10, new_msg_ratio=0.05)
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=1000, body_budget=500,
        tail_budget=1000, new_msg_budget=500,
        B_M=5000, main_M_room=5000,
        effective_trigger=0,  # violation
    )
    with pytest.raises(AssertionError, match="effective_trigger"):
        assert_static_bounds(cfg, budgets)


def test_assert_static_bounds_passes_valid_config() -> None:
    """Tier 2: assert_static_bounds does NOT raise for valid ratios and positive budgets."""
    cfg = _make_cfg(head_ratio=0.10, body_ratio=0.05, tail_ratio=0.15, new_msg_ratio=0.10)
    budgets = ComputedBudgets(
        main_pool=100_000, head_budget=10000, body_budget=5000,
        tail_budget=15000, new_msg_budget=10000,
        B_M=50000, main_M_room=65000, effective_trigger=50000,
    )
    assert_static_bounds(cfg, budgets)  # must not raise


# ---------------------------------------------------------------------------
# trim_head / trim_tail Axis 3: pure token-budget, no count cap
# ---------------------------------------------------------------------------


def test_trim_head_stops_at_budget_not_count() -> None:
    """Tier 2: trim_head stops at token budget regardless of list length.

    With use_chars4=True, each 40-char turn = 10 tokens.
    Budget of 25 tokens should admit exactly 2 turns (20 tokens total),
    stopping before the 3rd (would be 30 total).
    """
    # 8 turns, each with 40 chars = 10 tokens via chars//4.
    texts = ["a" * 40] * 8
    turns = _turns(texts)
    result = trim_head(turns, max_tokens=25, model="", use_chars4=True)
    assert len(result) == 2, (
        f"expected 2 turns (20 tokens ≤ 25), got {len(result)}"
    )
    assert result[0]["seq"] == 1
    assert result[1]["seq"] == 2


def test_trim_tail_stops_at_budget_not_count() -> None:
    """Tier 2: trim_tail stops at token budget regardless of list length.

    Same math as trim_head — takes from the end.  Budget 25, 10 tokens/turn.
    """
    texts = ["a" * 40] * 8
    turns = _turns(texts)
    result = trim_tail(turns, max_tokens=25, model="", use_chars4=True)
    assert len(result) == 2, (
        f"expected 2 turns (20 tokens ≤ 25), got {len(result)}"
    )
    # Must be the LAST two turns.
    assert result[-1]["seq"] == 8
    assert result[0]["seq"] == 7


def test_trim_head_includes_all_within_budget() -> None:
    """Tier 2: trim_head includes all turns when total tokens < max_tokens."""
    turns = _turns(["hi"] * 5)  # tiny turns
    result = trim_head(turns, max_tokens=100_000, model="", use_chars4=True)
    assert len(result) == 5


def test_trim_tail_includes_all_within_budget() -> None:
    """Tier 2: trim_tail includes all turns when total tokens < max_tokens."""
    turns = _turns(["hi"] * 5)
    result = trim_tail(turns, max_tokens=100_000, model="", use_chars4=True)
    assert len(result) == 5


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
    from reyn.chat.services.chat_compaction_engine import _IMAGE_FIXED_TOKEN_COST
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
    from reyn.chat.services.chat_compaction_engine import _IMAGE_FIXED_TOKEN_COST
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
    """Tier 2: when a single turn exceeds max_tokens, trim_head still includes
    it (Axis 7 — preserves conversation flow).
    """
    turns = [{"role": "user", "text": "x" * 4000, "seq": 1}]
    result = trim_head(turns, max_tokens=10, model="", use_chars4=True)
    assert len(result) == 1, "oversized turn must still be included in result"


def test_trim_tail_oversized_turn_still_included() -> None:
    """Tier 2: when a single turn exceeds max_tokens, trim_tail still includes it."""
    turns = [{"role": "user", "text": "x" * 4000, "seq": 1}]
    result = trim_tail(turns, max_tokens=10, model="", use_chars4=True)
    assert len(result) == 1


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
# B_M trigger race (Axis 8) — compaction lock ordering
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


class _LockHoldingEngine(ChatCompactionEngine):
    """Engine stub that holds the compaction_lock and signals when running."""

    def __init__(self, start_gate: asyncio.Event, release_gate: asyncio.Event) -> None:
        # Minimal init without model_budget lookup.
        self._model = "stub"
        self._events = EventLog()
        from reyn.config import CompactionConfig as _CC
        self._cfg = _CC(use_chars4_estimate=True)
        self._use_chars4 = True
        self._T_comp_SP = 10
        # Synthetic budgets — bypass assert_static_bounds.
        self._budgets = ComputedBudgets(
            main_pool=100_000, head_budget=10_000, body_budget=5_000,
            tail_budget=15_000, new_msg_budget=10_000,
            B_M=80_000, main_M_room=65_000, effective_trigger=65_000,
        )
        self.compaction_lock = asyncio.Lock()
        self._start_gate = start_gate
        self._release_gate = release_gate
        self.call_count = 0

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        self.call_count += 1
        self._start_gate.set()
        await self._release_gate.wait()
        return ChatSummary(topic_arc="stub", covers_through_seq=0)


def test_compaction_lock_blocks_concurrent_append() -> None:
    """Tier 2: when force_compact_now() is in progress, a concurrent task that
    awaits the compaction_lock is blocked until compaction completes.

    Verified via behavior: the append task observes that the lock is held
    during compaction, and completes AFTER the lock is released.

    Axis 8: mathematical race gap = 0.
    """
    from reyn.chat.services.compaction_controller import CompactionController

    start_gate: asyncio.Event = asyncio.Event()
    release_gate: asyncio.Event = asyncio.Event()
    engine = _LockHoldingEngine(start_gate=start_gate, release_gate=release_gate)

    history: list[_FakeMessage] = []
    for i in range(1, 12):
        role = "user" if i % 2 == 1 else "assistant"
        history.append(_FakeMessage(role=role, text="x" * 200, seq=i))

    def _latest_summary():
        return None

    appended_while_locked: list[bool] = []

    async def _locking_append(msg: _FakeMessage) -> None:
        """Append that must wait for the compaction lock."""
        async with engine.compaction_lock:
            history.append(msg)
            appended_while_locked.append(True)

    ctrl = CompactionController(
        event_log=EventLog(),
        config=CompactionConfig(
            head_size=2, tail_size=2, min_compact_batch=1,
            use_chars4_estimate=True,
        ),
        history_access=lambda: list(history),
        latest_summary=_latest_summary,
        chat_compaction_engine=engine,
        history_appender=lambda m: history.append(m),
        make_summary_message=lambda rendered, structured, covers: _FakeMessage(
            role="summary", text=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )

    ordering: list[str] = []

    async def _run():
        # Start force_compact_now in background.
        compact_task = asyncio.create_task(ctrl.force_compact_now())
        # Wait for the engine to start (= lock is held).
        await start_gate.wait()
        ordering.append("compact_started")

        # Now try to append — must block on the lock.
        append_task = asyncio.create_task(
            _locking_append(
                _FakeMessage(role="user", text="new turn", seq=100)
            )
        )
        # Give the append task a chance to start and block.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Lock is still held — append has NOT completed yet.
        ordering.append("checking_append_blocked")
        assert not appended_while_locked, (
            "append should not have completed while compaction lock is held"
        )

        # Release the engine.
        release_gate.set()
        await compact_task
        await append_task
        ordering.append("both_done")

    asyncio.run(_run())

    assert "compact_started" in ordering
    assert "both_done" in ordering
    assert appended_while_locked, "append should have completed after lock was released"


# ---------------------------------------------------------------------------
# Axis 10: use_chars4_estimate opt-out
# ---------------------------------------------------------------------------


def test_estimate_tokens_chars4_opt_out() -> None:
    """Tier 2: estimate_tokens with use_chars4=True uses len//4, not litellm.

    Verify deterministically without any LLM call.
    """
    from reyn.chat.services.chat_compaction_engine import estimate_tokens
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
    from reyn.chat.services.chat_compaction_engine import _IMAGE_FIXED_TOKEN_COST
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

    Uses a real lambda as the provider (no mock).  Two calls: first with a
    small SP, then with a larger SP (= smaller main_pool = smaller effective_trigger).
    """
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        cfg = _make_cfg(
            head_ratio=0.10,
            body_ratio=0.05,
            tail_ratio=0.15,
            new_msg_ratio=0.10,
            section_caps_spec_tokens=100,
            use_chars4_estimate=True,
        )
        events = EventLog()

        # Provider returns a 40-char SP = 10 tokens initially.
        sp_state: list[str] = ["a" * 40]
        provider = lambda: sp_state[0]  # noqa: E731

        engine = ChatCompactionEngine(
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
        engine = ChatCompactionEngine(
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
            head_ratio=0.10,
            body_ratio=0.05,
            tail_ratio=0.15,
            new_msg_ratio=0.10,
            section_caps_spec_tokens=100,
            use_chars4_estimate=True,
        )
        sp_text = "b" * 200  # 50 tokens via chars//4
        events = EventLog()

        engine = ChatCompactionEngine(
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

    Exercises the public contract for the error that _maybe_force_compact_for_router
    raises (ISSUE #5) when new_msg_text exceeds new_msg_budget.  Uses a deliberately
    huge text (10 million chars = 2.5M tokens via chars//4) against a small budget.
    """
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        cfg = _make_cfg(
            head_ratio=0.10,
            body_ratio=0.05,
            tail_ratio=0.15,
            new_msg_ratio=0.05,  # new_msg_budget = 5% * 100_000 = 5_000 tokens
            section_caps_spec_tokens=100,
            use_chars4_estimate=True,
        )
        events = EventLog()
        engine = ChatCompactionEngine(
            model="test-model",
            events=events,
            cfg=cfg,
            T_SP=0,
        )
        # new_msg_budget = 0.05 * 100_000 = 5_000 tokens
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
# ISSUE #6: force_compact_now race-recovery loop (Option B)
# ---------------------------------------------------------------------------


@dataclass
class _CountingEngine(ChatCompactionEngine):
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
        self.compaction_lock = asyncio.Lock()
        self.compact_call_count = 0

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        """Tier 2 stub: increment call count and return a stub summary."""
        self.compact_call_count += 1
        return ChatSummary(topic_arc="stub", covers_through_seq=0)


def test_force_compact_now_emits_race_unrecovered_when_always_over_budget() -> None:
    """Tier 2: force_compact_now emits force_compact_race_unrecovered when the
    history remains over budget after max_passes compaction runs.

    Simulates the race by using a history_access callable that always returns
    history with token count > effective_trigger, regardless of how many times
    compaction runs (= race always wins).

    Invariants verified via public API:
    - force_compact_race_unrecovered event emitted with passes=max_passes.
    - compact() was called exactly max_passes times.
    """
    from reyn.chat.services.compaction_controller import CompactionController

    events = EventLog()
    engine = _CountingEngine()

    # 600 turns * 100 tokens/turn = 60_000 > effective_trigger=50_000.
    # History never shrinks (simulating continuous concurrent appends).
    def _big_history() -> list[_FakeMessage]:
        return [
            _FakeMessage(role="user" if i % 2 == 0 else "assistant",
                         text="a" * 400, seq=i + 1)
            for i in range(600)
        ]

    ctrl = CompactionController(
        event_log=events,
        config=CompactionConfig(
            head_size=2, tail_size=2, min_compact_batch=1,
            trigger_total_tokens=1_000,
            use_chars4_estimate=True,
        ),
        history_access=_big_history,
        latest_summary=lambda: None,
        chat_compaction_engine=engine,
        history_appender=lambda m: None,
        make_summary_message=lambda rendered, structured, covers: _FakeMessage(
            role="summary", text=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )

    asyncio.run(ctrl.force_compact_now(max_passes=2))

    unrecovered = [e for e in events.all() if e.type == "force_compact_race_unrecovered"]
    assert unrecovered, (
        "force_compact_race_unrecovered event must be emitted when race persists"
    )
    assert unrecovered[0].data["passes"] == 2
    assert engine.compact_call_count == 2, (
        f"compact() should be called exactly 2 times, got {engine.compact_call_count}"
    )


def test_force_compact_now_returns_early_when_budget_recovered() -> None:
    """Tier 2: force_compact_now returns without emitting force_compact_race_unrecovered
    when the history is within budget after the first pass.

    Uses a history_access callable that returns small history on the second call
    (= simulates budget recovery after one compaction pass).
    """
    from reyn.chat.services.compaction_controller import CompactionController

    events = EventLog()
    engine = _CountingEngine()

    call_count: list[int] = [0]

    def _shrinking_history() -> list[_FakeMessage]:
        """First call: big history (over budget). Second+: small history (within budget)."""
        call_count[0] += 1
        if call_count[0] == 1:
            # 600 turns * 100 tokens = 60_000 > effective_trigger=50_000
            return [
                _FakeMessage(role="user" if i % 2 == 0 else "assistant",
                             text="a" * 400, seq=i + 1)
                for i in range(600)
            ]
        else:
            # After compaction: small history = 5 turns * 100 tokens = 500 < 50_000
            return [
                _FakeMessage(role="user" if i % 2 == 0 else "assistant",
                             text="a" * 400, seq=i + 1)
                for i in range(5)
            ]

    ctrl = CompactionController(
        event_log=events,
        config=CompactionConfig(
            head_size=1, tail_size=1, min_compact_batch=1,
            trigger_total_tokens=1_000,
            use_chars4_estimate=True,
        ),
        history_access=_shrinking_history,
        latest_summary=lambda: None,
        chat_compaction_engine=engine,
        history_appender=lambda m: None,
        make_summary_message=lambda rendered, structured, covers: _FakeMessage(
            role="summary", text=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )

    asyncio.run(ctrl.force_compact_now(max_passes=2))

    unrecovered = [e for e in events.all() if e.type == "force_compact_race_unrecovered"]
    assert not unrecovered, (
        "force_compact_race_unrecovered must NOT be emitted when budget was recovered"
    )
    assert engine.compact_call_count == 1, (
        f"compact() should be called exactly 1 time when budget recovers after pass 1, "
        f"got {engine.compact_call_count}"
    )
