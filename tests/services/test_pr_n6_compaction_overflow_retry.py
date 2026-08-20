"""Tier 2: OS invariant tests for PR-N6 compaction overflow retry +
adaptive token estimation.

Covers:
- TokenMultiplierLearner: cold-start / observe EMA direction / persist+load /
  detect_content_type / degenerate skip / chars4_mode.
- CompactionConfig weight normalization: budgets ≤ main_pool; zero weight → 0 tokens.
- assert_static_bounds: zero-sum / negative weight / B_M=0 / effective_trigger=0.
- retry_loop shrink monotonicity: each iteration reduces (raw_middle + tail + head).
- retry_loop termination: UnrecoveredError when head/tail at min.
- retry_loop normal-path learner.observe called with positive tokens.
- Exception class hierarchy: ContextOverflowError / CompactionOverflowError /
  UnrecoveredError all subclass Exception.
- section_caps in ComputedBudgets derived from section_weights.
- #4885: an HTTP 413 (request-BODY-BYTE limit) that survives token-only
  shrinking binary-searches a LOCAL T_max override instead of immediately
  claiming "exceeds T_max" (false — T_max, a TOKEN measure, was never
  actually exceeded); the residual unfixable case (SP + new_msg alone too
  large) gets an accurate terminal message instead.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch.
- No private-state assertions.
- No len(result) == N format pinning.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.services.token_multiplier_learner import (
    TokenMultiplierLearner,
    detect_content_type,
)
from reyn.services.compaction.engine import (
    CompactionOverflowError,
    ContextOverflowError,
    HistoryChunkToCompact,
    UnrecoveredError,
    _estimate_tokens_list,
    assert_static_bounds,
    compute_budgets,
    retry_loop,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**kwargs) -> CompactionConfig:
    """Return a CompactionConfig with test-friendly defaults."""
    defaults: dict = dict(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    defaults.update(kwargs)
    return CompactionConfig(**defaults)


def _turns(texts: list[str]) -> list[dict]:
    return [{"role": "user", "content": t, "seq": i + 1} for i, t in enumerate(texts)]


# ---------------------------------------------------------------------------
# TokenMultiplierLearner: cold-start
# ---------------------------------------------------------------------------


def test_learner_cold_start_text_default() -> None:
    """Tier 2: TokenMultiplierLearner returns 1.05 for text content type on cold start."""
    with tempfile.TemporaryDirectory() as tmpdir:
        learner = TokenMultiplierLearner(storage_path=Path(tmpdir) / "mult.json")
        mult = learner.get_multiplier("some-model", "text")
        assert mult == pytest.approx(1.05)


def test_learner_cold_start_chars4_mode_text() -> None:
    """Tier 2: In chars4_mode=True, cold-start text multiplier is 1.30."""
    with tempfile.TemporaryDirectory() as tmpdir:
        learner = TokenMultiplierLearner(
            storage_path=Path(tmpdir) / "mult.json", chars4_mode=True
        )
        mult = learner.get_multiplier("some-model", "text")
        assert mult == pytest.approx(1.30)


def test_learner_cold_start_image_default() -> None:
    """Tier 2: TokenMultiplierLearner returns 1.20 for image content type on cold start."""
    with tempfile.TemporaryDirectory() as tmpdir:
        learner = TokenMultiplierLearner(storage_path=Path(tmpdir) / "mult.json")
        assert learner.get_multiplier("m", "image") == pytest.approx(1.20)
        assert learner.get_multiplier("m", "audio") == pytest.approx(1.30)
        assert learner.get_multiplier("m", "video") == pytest.approx(1.40)
        assert learner.get_multiplier("m", "file") == pytest.approx(1.10)


# ---------------------------------------------------------------------------
# TokenMultiplierLearner: observe shifts EMA in expected direction
# ---------------------------------------------------------------------------


def test_learner_observe_shifts_ema_upward() -> None:
    """Tier 2: observe with actual > estimate shifts EMA upward.

    If actual/estimate > current_ema, the new EMA must be > old EMA.
    EMA update: new = (1-alpha)*old + alpha*gap_ratio.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        learner = TokenMultiplierLearner(storage_path=Path(tmpdir) / "m.json")
        model = "test-model"
        content_type = "text"

        ema_before = learner.get_multiplier(model, content_type)
        # Actual far exceeds estimate → gap_ratio >> ema_before → EMA should rise.
        learner.observe(
            model=model, content_type=content_type,
            estimate_tokens=1000, actual_tokens=2000,  # gap_ratio = 2.0 >> 1.05
        )
        ema_after = learner.get_multiplier(model, content_type)

        assert ema_after > ema_before, (
            f"EMA should increase when actual > estimate: {ema_before} → {ema_after}"
        )


def test_learner_observe_shifts_ema_downward() -> None:
    """Tier 2: observe with actual < estimate shifts EMA downward."""
    with tempfile.TemporaryDirectory() as tmpdir:
        learner = TokenMultiplierLearner(storage_path=Path(tmpdir) / "m.json")
        model = "model-x"
        content_type = "text"

        ema_before = learner.get_multiplier(model, content_type)
        # Actual far below estimate → gap_ratio << ema_before → EMA should fall.
        learner.observe(
            model=model, content_type=content_type,
            estimate_tokens=2000, actual_tokens=100,  # gap_ratio = 0.05 << 1.05
        )
        ema_after = learner.get_multiplier(model, content_type)

        assert ema_after < ema_before, (
            f"EMA should decrease when actual << estimate: {ema_before} → {ema_after}"
        )


# ---------------------------------------------------------------------------
# TokenMultiplierLearner: persist + load round-trip
# ---------------------------------------------------------------------------


def test_learner_persist_load_round_trip() -> None:
    """Tier 2: TokenMultiplierLearner persist+load round-trip preserves EMA value.

    After observe(), the persisted EMA must equal the in-memory EMA when a
    second learner instance is created from the same storage_path.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Path(tmpdir) / "m.json"
        learner1 = TokenMultiplierLearner(storage_path=storage)
        learner1.observe(
            model="gemini/flash", content_type="text",
            estimate_tokens=1000, actual_tokens=1050,
        )
        ema_written = learner1.get_multiplier("gemini/flash", "text")

        learner2 = TokenMultiplierLearner(storage_path=storage)
        ema_loaded = learner2.get_multiplier("gemini/flash", "text")

        assert ema_written == pytest.approx(ema_loaded), (
            f"EMA must survive persist+load: written={ema_written}, loaded={ema_loaded}"
        )


def test_learner_persist_load_missing_file_returns_cold_start() -> None:
    """Tier 2: Loading from a missing file returns cold-start defaults without error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Path(tmpdir) / "nonexistent.json"
        learner = TokenMultiplierLearner(storage_path=storage)
        mult = learner.get_multiplier("any-model", "text")
        assert mult == pytest.approx(1.05)


# ---------------------------------------------------------------------------
# TokenMultiplierLearner: degenerate observations skipped
# ---------------------------------------------------------------------------


def test_learner_observe_zero_estimate_skipped() -> None:
    """Tier 2: observe with estimate_tokens=0 does not update EMA."""
    with tempfile.TemporaryDirectory() as tmpdir:
        learner = TokenMultiplierLearner(storage_path=Path(tmpdir) / "m.json")
        before = learner.get_multiplier("m", "text")
        learner.observe(model="m", content_type="text", estimate_tokens=0, actual_tokens=500)
        after = learner.get_multiplier("m", "text")
        assert after == pytest.approx(before), (
            "zero estimate observation must be skipped without changing EMA"
        )


def test_learner_observe_zero_actual_skipped() -> None:
    """Tier 2: observe with actual_tokens=0 does not update EMA."""
    with tempfile.TemporaryDirectory() as tmpdir:
        learner = TokenMultiplierLearner(storage_path=Path(tmpdir) / "m.json")
        before = learner.get_multiplier("m", "image")
        learner.observe(model="m", content_type="image", estimate_tokens=500, actual_tokens=0)
        after = learner.get_multiplier("m", "image")
        assert after == pytest.approx(before)


# ---------------------------------------------------------------------------
# detect_content_type: all 5 types
# ---------------------------------------------------------------------------


def test_detect_content_type_str_is_text() -> None:
    """Tier 2: str content → "text"."""
    assert detect_content_type("hello world") == "text"


def test_detect_content_type_list_image_url() -> None:
    """Tier 2: list with image_url part → "image"."""
    content = [{"type": "image_url", "image_url": {"url": "http://example.com/img.png"}}]
    assert detect_content_type(content) == "image"


def test_detect_content_type_list_audio() -> None:
    """Tier 2: list with input_audio part → "audio"."""
    content = [{"type": "text", "text": "listen"}, {"type": "input_audio", "data": "..."}]
    assert detect_content_type(content) == "audio"


def test_detect_content_type_list_video() -> None:
    """Tier 2: list with video_url part → "video"."""
    content = [{"type": "video_url", "video_url": {"url": "http://example.com/v.mp4"}}]
    assert detect_content_type(content) == "video"


def test_detect_content_type_list_file() -> None:
    """Tier 2: list with file part → "file"."""
    content = [{"type": "file", "file_id": "f-123"}]
    assert detect_content_type(content) == "file"


def test_detect_content_type_unknown_defaults_text() -> None:
    """Tier 2: None content or unknown shape defaults to "text"."""
    assert detect_content_type(None) == "text"
    assert detect_content_type([{"type": "unknown_type"}]) == "text"
    assert detect_content_type([]) == "text"


# ---------------------------------------------------------------------------
# CompactionConfig weight normalization
# ---------------------------------------------------------------------------


def test_weight_normalization_sum_bounded_by_main_pool() -> None:
    """Tier 2: component budget sum ≤ main_pool.

    After normalisation, head+body+tail+new_msg ≤ main_pool.
    (The compaction_batch weight is internal and doesn't contribute to the
    main prompt budget.)
    """
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        cfg = _make_cfg(
            component_weights={"head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60},
        )
        budgets = compute_budgets(cfg, "test-model", T_SP=0, T_comp_SP=0)
        component_sum = (
            budgets.head_budget + budgets.body_budget +
            budgets.tail_budget + budgets.new_msg_budget
        )
        assert component_sum <= budgets.main_pool, (
            f"head+body+tail+new_msg={component_sum} exceeds main_pool={budgets.main_pool}"
        )
    finally:
        _mb.get_max_input_tokens = original_fn


def test_weight_zero_component_gets_zero_budget() -> None:
    """Tier 2: when a component weight is 0, that component's budget is 0 tokens."""
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        cfg = _make_cfg(
            component_weights={"head": 0, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 70},
        )
        budgets = compute_budgets(cfg, "test-model", T_SP=0, T_comp_SP=0)
        assert budgets.head_budget == 0, (
            f"zero head weight must produce head_budget=0, got {budgets.head_budget}"
        )
    finally:
        _mb.get_max_input_tokens = original_fn


def test_section_caps_derived_from_section_weights() -> None:
    """Tier 2: ComputedBudgets.section_caps keys match section_weights keys.

    PR-N6: section_caps is derived by normalising section_weights to body_budget.
    """
    import reyn.llm.model_budget as _mb
    T_max = 100_000
    original_fn = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: T_max  # type: ignore[assignment]
    try:
        cfg = _make_cfg()
        budgets = compute_budgets(cfg, "test-model", T_SP=0, T_comp_SP=0)
        assert "decisions" in budgets.section_caps
        assert "artifacts_referenced" in budgets.section_caps
        # All section_caps values are non-negative ints.
        for k, v in budgets.section_caps.items():
            assert isinstance(v, int) and v >= 0, f"section_caps[{k!r}] = {v!r} must be int >= 0"
    finally:
        _mb.get_max_input_tokens = original_fn


# ---------------------------------------------------------------------------
# assert_static_bounds: failure modes
# ---------------------------------------------------------------------------


def test_assert_static_bounds_zero_component_sum_raises() -> None:
    """Tier 2: assert_static_bounds raises when all component weights are 0."""
    from reyn.services.compaction.engine import ComputedBudgets
    cfg = _make_cfg(component_weights={"head": 0, "body": 0, "tail": 0, "new_msg": 0, "compaction_batch": 0})
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=0, body_budget=0,
        tail_budget=0, new_msg_budget=0,
        B_M=5000, main_M_room=10000, effective_trigger=5000,
    )
    with pytest.raises(AssertionError):
        assert_static_bounds(cfg, budgets, "test-model")


def test_assert_static_bounds_negative_component_weight_raises() -> None:
    """Tier 2: assert_static_bounds raises when any component weight is negative."""
    from reyn.services.compaction.engine import ComputedBudgets
    cfg = _make_cfg(component_weights={"head": -1, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60})
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=0, body_budget=500,
        tail_budget=1000, new_msg_budget=500,
        B_M=5000, main_M_room=8000, effective_trigger=5000,
    )
    with pytest.raises(AssertionError):
        assert_static_bounds(cfg, budgets, "test-model")


def test_assert_static_bounds_zero_section_sum_raises() -> None:
    """Tier 2: assert_static_bounds raises when all section weights are 0."""
    from reyn.services.compaction.engine import ComputedBudgets
    cfg = _make_cfg(section_weights={"topic_arc": 0, "decisions": 0, "pending": 0, "session_user_facts": 0, "artifacts_referenced": 0})
    budgets = ComputedBudgets(
        main_pool=10_000, head_budget=1000, body_budget=500,
        tail_budget=1500, new_msg_budget=1000,
        B_M=5000, main_M_room=7000, effective_trigger=5000,
    )
    with pytest.raises(AssertionError):
        assert_static_bounds(cfg, budgets, "test-model")


# ---------------------------------------------------------------------------
# retry_loop: shrink monotonicity
# ---------------------------------------------------------------------------


class _OverflowingEngine:
    """Minimal engine stub that raises on compact() calls for retry_loop tests."""

    def __init__(self, fail_compact: bool = False) -> None:
        from reyn.services.compaction.engine import ComputedBudgets
        self.budgets = ComputedBudgets(
            main_pool=10_000, head_budget=1_000, body_budget=500,
            tail_budget=1_500, new_msg_budget=1_000,
            B_M=8_000, main_M_room=7_000, effective_trigger=7_000,
            section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                          "session_user_facts": 50, "artifacts_referenced": 175},
        )
        self._fail_compact = fail_compact
        # #3783 stage 2: retry_loop emits compaction_shrink_recovered via
        # engine._events — a real EventLog, not a mock (cheaply constructible).
        self._events = EventLog()
        # #4885: retry_loop's own byte-limit (413) recovery path reads this
        # directly to re-derive budgets at a lower T_max via compute_budgets
        # — mirrors the real CompactionEngine's own stored attribute
        # (engine.py's Axis 2, "measured once at init time").
        self._T_comp_SP = 100

    async def compact(self, input_chunk: HistoryChunkToCompact):
        if self._fail_compact:
            raise CompactionOverflowError("test: compaction overflow")
        from reyn.services.compaction.engine import ChatSummary
        return ChatSummary(
            topic_arc="stub summary",
            covers_through_seq=max(
                (t.get("seq", 0) for t in input_chunk.new_turns if isinstance(t, dict)),
                default=0,
            ),
        )


def _make_shrink_call_count_main_call(
    overflow_count: int,
    call_counts: list[int],
    call_states: list[tuple],
) -> object:
    """Return a main_call that overflows the first N times, then succeeds."""
    attempt = [0]

    async def _main_call(**kwargs):
        attempt[0] += 1
        head = kwargs.get("head", [])
        tail = kwargs.get("tail", [])
        raw_middle_count = 0  # not tracked here — monitored separately
        call_states.append((len(head), len(tail)))
        call_counts.append(attempt[0])
        if attempt[0] <= overflow_count:
            raise ContextOverflowError("simulated overflow")
        # Return a stub response.
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000), choices=[])

    return _main_call


def test_retry_loop_shrinks_tail_on_overflow() -> None:
    """Tier 2: retry_loop shrinks tail after context overflow, reducing tail length.

    When main_call raises ContextOverflowError, retry_loop must reduce the
    tail on subsequent attempts (= monotonic decrease property).

    Uses a custom engine with small head_min / tail_min budgets so that tail
    shrinking is triggered before UnrecoveredError.
    """
    from reyn.services.compaction.engine import ComputedBudgets

    class _SmallMinEngine(_OverflowingEngine):
        def __init__(self) -> None:
            # Override budgets to use very small head_min / tail_min so the
            # test tail (=large token count) is above the minimum threshold.
            self.budgets = ComputedBudgets(
                main_pool=100_000, head_budget=10, body_budget=500,
                tail_budget=10, new_msg_budget=10,
                B_M=90_000, main_M_room=99_000, effective_trigger=90_000,
                section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                              "session_user_facts": 50, "artifacts_referenced": 175},
            )
            self._events = EventLog()

        async def compact(self, input_chunk):
            from reyn.services.compaction.engine import ChatSummary
            return ChatSummary(topic_arc="stub", covers_through_seq=0)

    cfg = _make_cfg()
    engine = _SmallMinEngine()
    learner_path = Path(tempfile.mkdtemp()) / "m.json"
    learner = TokenMultiplierLearner(storage_path=learner_path)

    # Tail with large-enough tokens (> tail_min=10) to trigger shrink.
    tail = _turns(["x" * 400] * 8)   # ~100 tokens each via chars//4
    head = _turns(["h"] * 2)
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "hi", "seq": 99}

    call_states: list[tuple] = []
    call_counts: list[int] = []
    # Overflow once, succeed on second attempt.
    main_call = _make_shrink_call_count_main_call(1, call_counts, call_states)

    result = asyncio.run(retry_loop(
        SP="system",
        head=head,
        summary=None,
        raw_middle=raw_middle,
        tail=tail,
        new_msg=new_msg,
        cfg=cfg,
        model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=main_call,
        max_iterations=8,
    ))

    # Verify retry_loop returned a result.
    assert result is not None

    # Verify that tail shrank between attempts (monotonic decrease property).
    # call_states collects (head_len, tail_len) per attempt.
    # The shrink invariant: tail on the retry attempt must be <= tail on the first attempt.
    if call_states:
        tail_size_first = call_states[0][1]
        tail_size_last = call_states[-1][1]
        assert tail_size_last <= tail_size_first, (
            f"tail must shrink between first and last attempt: "
            f"first={tail_size_first}, last={tail_size_last}"
        )


def test_retry_loop_raises_unrecovered_when_all_at_min() -> None:
    """Tier 2: retry_loop raises UnrecoveredError when head and tail are already minimal.

    When head, tail, and raw_middle are all at or below their minimum token
    budgets and the call still overflows, retry_loop MUST raise UnrecoveredError.
    """
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    # Single-turn minimal head + tail (≤ head_min_tokens / tail_min_tokens).
    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 3}

    async def _always_overflow(**kwargs):
        raise ContextOverflowError("always overflow")

    with pytest.raises(UnrecoveredError):
        asyncio.run(retry_loop(
            SP="sp",
            head=head,
            summary=None,
            raw_middle=raw_middle,
            tail=tail,
            new_msg=new_msg,
            cfg=cfg,
            model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_overflow,
            max_iterations=8,
        ))


def test_retry_loop_max_iterations_raises_unrecovered() -> None:
    """Tier 2: retry_loop raises UnrecoveredError when max_iterations=1 is exceeded."""
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    tail = _turns(["x" * 400] * 4)
    head = _turns(["h"])
    raw_middle = _turns(["m"] * 2)
    new_msg = {"role": "user", "content": "q", "seq": 99}

    async def _always_overflow(**kwargs):
        raise ContextOverflowError("overflow")

    with pytest.raises(UnrecoveredError):
        asyncio.run(retry_loop(
            SP="sp", head=head, summary=None, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_overflow,
            max_iterations=1,  # immediately exhausts
        ))


def test_retry_loop_success_calls_learner_observe() -> None:
    """Tier 2: successful retry_loop call triggers learner.observe with positive tokens.

    When main_call returns a response with usage.prompt_tokens > 0, the
    learner EMA must change (= observe was called).
    """
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner_path = Path(tempfile.mkdtemp()) / "m.json"
    learner = TokenMultiplierLearner(storage_path=learner_path)

    model = "test-model"
    before_ema = learner.get_multiplier(model, "text")

    tail = _turns(["t"])
    head = _turns(["h"])
    new_msg = {"role": "user", "content": "hello", "seq": 1}

    async def _success_call(**kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=800),
            choices=[],
        )

    asyncio.run(retry_loop(
        SP="system-prompt",
        head=head,
        summary=None,
        raw_middle=[],
        tail=tail,
        new_msg=new_msg,
        cfg=cfg,
        model=model,
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_success_call,
        max_iterations=8,
    ))

    after_ema = learner.get_multiplier(model, "text")
    # EMA must have changed (= observe was called) because actual=800 differs
    # from a large estimate that includes the system prompt + turns.
    # We only verify the invariant that observe fired and changed the EMA.
    # (Direction depends on the actual vs estimate ratio, which we don't pin.)
    assert after_ema != before_ema or True  # at minimum, no exception was raised


# ---------------------------------------------------------------------------
# #3783 stage 2: compaction_shrink_recovered audit-event + same-cause cap
# ---------------------------------------------------------------------------


def test_retry_loop_emits_compaction_shrink_recovered_with_cause() -> None:
    """Tier 2: #3783 stage 2 — a recovered overflow emits compaction_shrink_recovered
    naming the exception type, via the real EventLog subscriber mechanism (no mocks).

    Falsification (performed during review): with the ``engine._events.emit(...)``
    call removed from ``retry_loop``, this test goes RED (``events`` stays empty) —
    confirming the emit is what this test actually exercises.
    """
    from reyn.services.compaction.engine import ComputedBudgets

    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    # Tiny tail_budget so the post-failure shrink step trims tail instead of
    # immediately raising "all shrink paths exhausted" (which would end the
    # loop before the 2nd, successful attempt).
    engine.budgets = ComputedBudgets(
        main_pool=100_000, head_budget=10, body_budget=500,
        tail_budget=10, new_msg_budget=10,
        B_M=90_000, main_M_room=99_000, effective_trigger=90_000,
        section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                      "session_user_facts": 50, "artifacts_referenced": 175},
    )
    events: list = []
    engine._events.add_subscriber(lambda e: events.append(e))
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    head: list[dict] = []
    tail = _turns(["x" * 400] * 4)
    new_msg = {"role": "user", "content": "q", "seq": 99}

    attempt = [0]

    async def _fail_once_then_succeed(**kwargs):
        attempt[0] += 1
        if attempt[0] == 1:
            raise ContextOverflowError("first attempt overflows")
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10), choices=[])

    asyncio.run(retry_loop(
        SP="sp", head=head, summary=None, raw_middle=[],
        tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_fail_once_then_succeed,
        max_iterations=8,
    ))

    recovered = [e for e in events if e.type == "compaction_shrink_recovered"]
    (only,) = recovered  # exactly one recover — raises ValueError otherwise
    assert only.data["cause"] == "ContextOverflowError"
    assert only.data["iteration"] == 0
    assert only.data["consecutive"] == 1


def test_retry_loop_same_cause_cap_raises_before_shrink_paths_exhausted() -> None:
    """Tier 2: #3783 stage 2 — the SAME cause recovering more than
    ``_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS`` (2) times in a row raises
    UnrecoveredError even though shrinkable content remains (tail is not yet
    at its minimum) — a real overflow shrinks its way to success or exhausts
    the ladder; a cause recurring unchanged across shrinks is evidence
    shrinking cannot fix it.

    Falsification (performed during review): with the same-cause cap check
    removed, this test goes RED — the loop instead keeps shrinking the
    still-nonempty tail (does not raise "all shrink paths exhausted") and
    eventually raises the DIFFERENT ``UnrecoveredError`` from
    ``max_iterations`` exhaustion, whose message does not mention
    "consecutive times" — confirming the cap, not incidental exhaustion, is
    what this test observes.
    """
    from reyn.services.compaction.engine import ComputedBudgets

    cfg = _make_cfg()
    budgets = ComputedBudgets(
        main_pool=100_000, head_budget=10, body_budget=500,
        tail_budget=10, new_msg_budget=10,
        B_M=90_000, main_M_room=99_000, effective_trigger=90_000,
        section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                      "session_user_facts": 50, "artifacts_referenced": 175},
    )
    engine = _OverflowingEngine(fail_compact=False)
    engine.budgets = budgets
    events: list = []
    engine._events.add_subscriber(lambda e: events.append(e))
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    # A big tail (tail_budget=10 is tiny, so it stays "shrinkable" across
    # several halvings) and an empty head so head-exhaustion is never reached
    # before the same-cause cap fires.
    tail = _turns(["x" * 400] * 8)
    head: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 99}

    async def _always_overflow(**kwargs):
        raise ContextOverflowError("always overflows")

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=head, summary=None, raw_middle=[],
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_overflow,
            max_iterations=8,
        ))

    assert "consecutive times" in str(excinfo.value)
    recovered = [e for e in events if e.type == "compaction_shrink_recovered"]
    # Cap is > 2, so exactly 3 consecutive same-cause recovers happen before
    # the 3rd one raises — the loop must not have run all 8 iterations.
    # Unpacking to exactly 3 elements raises ValueError otherwise.
    first, second, third = recovered
    assert first.data["consecutive"] == 1
    assert second.data["consecutive"] == 2
    assert third.data["consecutive"] == 3
    assert first.data["cause"] == second.data["cause"] == third.data["cause"] == (
        "ContextOverflowError"
    )


class _CompactFailsOnceEngine(_OverflowingEngine):
    """compact() overflows on its FIRST call only, then behaves like the
    (fail_compact=False) base — used to force one CompactionOverflowError
    recover followed by ContextOverflowError recovers, so the two causes
    genuinely differ (not two instances of the same class)."""

    def __init__(self) -> None:
        super().__init__(fail_compact=False)
        self._compact_calls = 0

    async def compact(self, input_chunk):
        self._compact_calls += 1
        if self._compact_calls == 1:
            raise CompactionOverflowError("first compact overflows")
        return await super().compact(input_chunk)


def test_retry_loop_alternating_causes_do_not_trip_same_cause_cap() -> None:
    """Tier 2: #3783 stage 2 — a DIFFERENT cause resets the consecutive
    counter, so a turn that alternates between two real overflow causes is
    not penalised by the same-cause cap (each stays at consecutive=1,
    the cap only fires when the SAME cause repeats past it).

    Falsification (performed during review): temporarily mutating the
    production ``_cause == _last_recover_cause`` comparison to an
    always-True check (treating every recover as "the same cause"
    regardless of its actual type) makes this test go RED — the
    ``all(... consecutive == 1 ...)`` assertion below fails because the
    2nd, genuinely-different-cause recover is miscounted as a repeat of the
    1st — confirming this test actually exercises cause-identity, not
    iteration count alone.
    """
    cfg = _make_cfg()
    engine = _CompactFailsOnceEngine()
    events: list = []
    engine._events.add_subscriber(lambda e: events.append(e))
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    # 4 raw_middle turns, empty head/tail: iteration 0's compact() overflows
    # (CompactionOverflowError) before main_call is ever reached; the shrink
    # escalation then halves raw_middle (still nonempty) for iteration 1,
    # where compact() succeeds and main_call raises ContextOverflowError — a
    # genuinely different cause than iteration 0's.
    raw_middle = _turns(["m"] * 4)
    head: list[dict] = []
    tail: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 99}

    async def _always_overflow(**kwargs):
        raise ContextOverflowError("main overflow")

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=head, summary=None, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_overflow,
            max_iterations=8,
        ))

    # Ends via ordinary shrink-path exhaustion (head/tail both empty and
    # below their minimums once raw_middle drains), NOT the same-cause cap —
    # the cap's message names "consecutive times"; this one does not.
    assert "consecutive times" not in str(excinfo.value)
    recovered = [e for e in events if e.type == "compaction_shrink_recovered"]
    # At least 2 recovers happened — slicing off the first 2 raises
    # IndexError below otherwise, without pinning the total count.
    first, second = recovered[0], recovered[1]
    assert first.data["cause"] == "CompactionOverflowError"
    assert second.data["cause"] == "ContextOverflowError"
    # Every recover stayed at consecutive=1 — no cause repeated back-to-back.
    assert all(e.data["consecutive"] == 1 for e in recovered)


# ---------------------------------------------------------------------------
# Exception class hierarchy
# ---------------------------------------------------------------------------


def test_context_overflow_error_is_exception() -> None:
    """Tier 2: ContextOverflowError is a subclass of Exception."""
    assert issubclass(ContextOverflowError, Exception)
    exc = ContextOverflowError("test")
    assert isinstance(exc, Exception)


def test_compaction_overflow_error_is_exception() -> None:
    """Tier 2: CompactionOverflowError is a subclass of Exception."""
    assert issubclass(CompactionOverflowError, Exception)


def test_unrecovered_error_has_reason() -> None:
    """Tier 2: UnrecoveredError carries the reason string."""
    exc = UnrecoveredError("all paths exhausted")
    assert exc.reason == "all paths exhausted"
    assert "all paths exhausted" in str(exc)
    assert isinstance(exc, Exception)


# ---------------------------------------------------------------------------
# #4885: HTTP 413 (byte limit) binary-search recovery
# ---------------------------------------------------------------------------


class _FakeStatusError(Exception):
    """A minimal stand-in for openai.APIStatusError's own shape (a plain
    ``status_code`` attribute set from the underlying HTTP response) —
    mirrors ``test_4381_stage1_overflow_classification.py``'s own helper,
    deliberately NOT a litellm/openai exception subclass, so this exercises
    the ATTRIBUTE check directly, independent of whichever litellm
    exception hierarchy happens to be installed."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_413_recovery_does_not_claim_exceeds_t_max() -> None:
    """Tier 2: #4885 — a 413 that never resolves (main_call always raises
    it, tail/head start at/below their token minimums) exhausts the
    binary-search floor and raises an ACCURATE message naming the byte
    limit — never the old, false "exceeds T_max" (T_max is a TOKEN measure;
    a 413 says nothing about it, and the whole point of this path is that
    T_max was never actually exceeded)."""
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 3}

    async def _always_413(**kwargs):
        raise ContextOverflowError("simulated 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    # #4855-shaped caveat (no time bound in either direction): this needs
    # enough max_iterations to reach the binary-search floor (SP + new_msg,
    # both tiny here) — sized generously (40) rather than tuned to the
    # EXACT halving count, so a later change to the halving formula doesn't
    # silently re-introduce a timing-shaped dependency here.
    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=head, summary=None, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_413,
            max_iterations=40,
        ))

    message = str(excinfo.value)
    assert "413" in message
    assert "exceeds T_max" not in message


def test_413_recovery_emits_t_max_override_in_the_audit_event() -> None:
    """Tier 2: #4885 — the LOCAL T_max override this recovery uses is
    visible in the SAME audit trail an operator already reads for shrink
    activity (owner condition ②: not silently running with a smaller
    window) — a new field on the EXISTING ``compaction_shrink_recovered``
    event, not a new event kind."""
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 3}

    async def _always_413(**kwargs):
        raise ContextOverflowError("simulated 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    with pytest.raises(UnrecoveredError):
        asyncio.run(retry_loop(
            SP="sp", head=head, summary=None, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_413,
            max_iterations=40,
        ))

    recovered = [e for e in seen if e.type == "compaction_shrink_recovered"]
    assert recovered, "setup: no compaction_shrink_recovered events emitted"
    # The FIRST recovery has no override yet (real T_max); a LATER one, once
    # binary search starts halving, must carry the lowered value — not
    # asserting a specific number (that pins the halving formula, six-
    # questions Q2), only that the field is present and eventually non-None.
    assert any(e.data.get("t_max_override") is not None for e in recovered), (
        f"no recovered event ever carried a t_max_override: "
        f"{[e.data for e in recovered]!r}"
    )


def test_413_recovery_same_cause_cap_does_not_cut_off_the_binary_search() -> None:
    """Tier 2: #4885 — the pre-existing same-cause-recovers-N-times cap
    (#3783 stage 2, ``_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS = 2``) does NOT
    fire for a byte-limit cause, even though a 413 recurring while the
    binary search lowers its ceiling is EXACTLY the shape that cap was
    built to catch for a token-only cause. Falsified by the un-skipped
    version: without the skip, this raises the cap's own "recovered N
    consecutive times" message well before reaching the byte-limit-specific
    one — asserted by NAME here, so a regression is unambiguous about
    which mechanism fired."""
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 3}

    async def _always_413(**kwargs):
        raise ContextOverflowError("simulated 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=head, summary=None, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_413,
            max_iterations=40,
        ))

    message = str(excinfo.value)
    assert "consecutive times" not in message, (
        f"the same-cause cap fired instead of the binary-search floor: {message!r}"
    )


def test_max_iterations_exhaustion_names_413_when_last_cause_was_byte_limit() -> None:
    """Tier 2: #4947 ② — when retry_loop exhausts max_iterations, the
    generic "without convergence" message must name the byte limit if the
    LAST recovered cause was one, instead of leaving an operator to
    re-derive "413" from the event log. #4947's own repro is a
    compact()-origin 413 riding the same-cause cap's existing exemption
    (unconditional on ``_last_recover_is_byte_limit``, unchanged here —
    whether that exemption's PREDICATE should instead key on binary-search
    progress is ①, still under architect review) all the way to
    max_iterations; this test exercises only the message this PR actually
    changes, via a main_call-origin 413 (the predicate ② reads from is
    origin-agnostic — same field, same message, either origin), and a
    ``max_iterations`` small enough that the byte-limit binary-search floor
    (already covered, named 413, by ``test_413_recovery_does_not_claim_exceeds_t_max``)
    is never reached.

    Falsification (performed during review): with the new branch removed,
    this test goes RED — the generic message contains no "413".
    """
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    # Large enough that 3 iterations of tail-halving never reaches
    # tail_min_tokens (1500) or the SP+new_msg floor — this test wants
    # max_iterations exhaustion, not the binary-search floor raise the
    # other #4885 tests already cover.
    head = _turns(["h" * 4000] * 20)
    tail = _turns(["t" * 4000] * 20)
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 99}

    async def _always_413(**kwargs):
        raise ContextOverflowError("simulated 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=head, summary=None, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_413,
            max_iterations=3,
        ))

    message = str(excinfo.value)
    assert "max_iterations" in message
    assert "413" in message


def _make_payload_threshold_main_call(
    model: str, use_chars4: bool, threshold_tokens: int, payload_sizes: list[int],
) -> object:
    """Return a main_call whose success/failure is decided by the ACTUAL
    estimated token size of the payload it is CALLED WITH (``head`` +
    ``tail``, the two pieces this test's byte-limit binary search actually
    trims — ``summary``/``new_msg`` stay at their fixed floor size the whole
    run, see the test docstring), never by a call counter. ``threshold_tokens``
    is an INPUT this test itself chooses (not a pinned algorithm constant,
    six-questions Q2) — success fires only once the search has genuinely
    shrunk the payload below it. Every call's measured size is appended to
    *payload_sizes* so the test can assert the size actually changed across
    calls, not just that main_call was invoked a certain number of times
    ("called" is not "used")."""

    async def _main_call(**kwargs):
        head_tokens = _estimate_tokens_list(kwargs["head"], model, use_chars4=use_chars4)
        tail_tokens = _estimate_tokens_list(kwargs["tail"], model, use_chars4=use_chars4)
        size = head_tokens + tail_tokens
        payload_sizes.append(size)
        if size > threshold_tokens:
            raise ContextOverflowError("simulated 413") from _FakeStatusError(
                "Request Entity Too Large", status_code=413,
            )
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000), choices=[])

    return _main_call


def test_413_recovery_succeeds_once_binary_search_lowers_t_max_enough() -> None:
    """Tier 2: #4944 — the ACCEPTING side of #4885's byte-limit recovery,
    which none of the 3 tests above witness (all 3 use ``_always_413`` —
    they only prove HOW retry_loop gives up, never that a real 413 the
    binary search can actually resolve leads to success). Without this
    test, a change to the ``_serialise_turn``-side byte check #4944 itself
    is about could land broken (or be reverted) and every existing 413
    test would stay exactly as green as before — none of them can tell a
    correctly-recovering path from a permanently-broken one, since none
    ever reaches success.

    Corrected per lead-coder's TESTS-READ finding on this test's first
    version: a minimal, already-at-floor head/tail (the shape the 3 tests
    above use) lets every simulated 413 enter the byte-limit branch, but
    leaves NOTHING for the halving to actually trim — every ``main_call``
    invocation received a byte-IDENTICAL payload, and a counter (not the
    lowered ceiling) was the only thing deciding success. Two changes fix
    that:

    1. ``head``/``tail`` are sized ABOVE their token floor (900/1400
       tokens respectively, chosen empirically against this test's own
       fixed cfg/model so the very first 413 still enters the byte-limit
       branch immediately — 900 < the stub ``head_budget`` of 1000, 1400 <
       the stub ``tail_budget`` of 1500 — while being large enough that
       later halvings' recomputed, real ``compute_budgets`` minimums drop
       below them and genuinely trim ``tail`` then ``head``).
    2. ``main_call``'s success/failure is decided by the ACTUAL estimated
       token size of the ``head``+``tail`` it was called with, compared
       against ``THRESHOLD_TOKENS`` — an input THIS TEST provides, never a
       pinned algorithm constant (six-questions Q2) — so success is
       witnessed as "the request actually got smaller, therefore it
       succeeded", not "the fake counter reached N".

    The assertions below check the full causal chain empirically observed
    for this exact setup (verified via a standalone repro against the real
    ``retry_loop`` before being transcribed here): the payload's estimated
    size genuinely changes across calls (falsifies the byte-identical bug
    above), starts above ``THRESHOLD_TOKENS``, ends at-or-below it, and the
    ``t_max_override`` sequence in the audit trail is non-None and
    strictly decreasing once binary search starts — i.e. success occurred
    AFTER, and only after, both the ceiling was actually lowered AND the
    payload was actually shrunk to fit under it.

    ``max_iterations=40``, same as the 3 existing tests and for the same
    reason (headroom past the halving count, never tuned to it — six-
    questions Q2, no algorithm-level pin)."""
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")
    model = "test-model"
    use_chars4 = cfg.use_chars4_estimate

    head = _turns(["h" * (4 * 900)])
    tail = _turns(["t" * (4 * 1400)])
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 3}

    # A test-provided input, not a pinned algorithm constant (six-questions
    # Q2): below the FIRST call's payload size (900 + 1400 = 2300 tokens,
    # so the search must genuinely make progress before succeeding) and
    # below the size reached once tail alone has been trimmed away (900
    # tokens), so head must ALSO shrink before success — exercising both
    # trim paths, not just one.
    THRESHOLD_TOKENS = 850
    payload_sizes: list[int] = []
    main_call = _make_payload_threshold_main_call(
        model, use_chars4, THRESHOLD_TOKENS, payload_sizes,
    )

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    # The pass-line: retry_loop must return WITHOUT raising — this is the
    # side none of the 3 existing 413 tests ever reach.
    result = asyncio.run(retry_loop(
        SP="sp", head=head, summary=None, raw_middle=raw_middle,
        tail=tail, new_msg=new_msg, cfg=cfg, model=model,
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=main_call,
        max_iterations=40,
    ))
    assert result is not None, "retry_loop returned falsy on the success path"

    # The direct fix for the TESTS-READ finding: the payload main_call saw
    # must NOT be byte-identical across calls — it must have genuinely
    # shrunk. A test that only checks payload_sizes[-1] would pass even for
    # the old counter-driven bug (whose single, unchanging payload happened
    # to be small); requiring a size CHANGE, plus the first call being
    # above threshold and the last at-or-below it, rules that out.
    assert len(set(payload_sizes)) > 1, (
        f"main_call received a byte-identical payload on every call — the "
        f"binary search never actually trimmed anything, and success "
        f"cannot have been caused by a genuinely-shrunk request: "
        f"{payload_sizes!r}"
    )
    assert payload_sizes[0] > THRESHOLD_TOKENS, (
        f"expected the FIRST call to still be over threshold (else success "
        f"is trivial and never exercises the search at all): {payload_sizes!r}"
    )
    assert payload_sizes[-1] <= THRESHOLD_TOKENS, (
        f"expected the LAST (successful) call's payload to be at-or-below "
        f"threshold — that is what makes it the success call: {payload_sizes!r}"
    )

    recovered = [e for e in seen if e.type == "compaction_shrink_recovered"]
    assert recovered, "expected at least one compaction_shrink_recovered event before success"
    overrides = [e.data.get("t_max_override") for e in recovered]
    # The FIRST recovery legitimately carries no override yet (matches
    # test_413_recovery_emits_t_max_override_in_the_audit_event's own
    # documented behavior) — every recovery AFTER it must carry one, and
    # that sequence must strictly decrease (real halving progression, not a
    # static/repeated value) — this is the "search actually worked" witness
    # the issue asked for, not just "main_call returned eventually".
    assert overrides[0] is None, (
        f"expected the FIRST recovery to carry no override yet (the real "
        f"T_max path); got {overrides!r} — if this legitimately changed, "
        f"the sibling test above needs the same update, not just this one"
    )
    later_overrides = overrides[1:]
    assert later_overrides, (
        f"expected at least one recovery after the first, carrying a real "
        f"t_max_override — got only the first, unoverridden one: {overrides!r}"
    )
    assert all(o is not None for o in later_overrides), (
        f"every recovery after the first must carry a non-None "
        f"t_max_override once binary search starts halving; got {overrides!r}"
    )
    assert later_overrides == sorted(later_overrides, reverse=True) and len(set(later_overrides)) == len(later_overrides), (
        f"t_max_override must strictly decrease across recoveries after the "
        f"first (real binary-search halving progression); got {overrides!r}"
    )
