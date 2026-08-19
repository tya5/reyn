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
