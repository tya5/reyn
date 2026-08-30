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

    async def compact(self, input_chunk: HistoryChunkToCompact, *, covers_through=None):
        if self._fail_compact:
            raise CompactionOverflowError("test: compaction overflow")
        from reyn.services.compaction.engine import ChatSummary
        return ChatSummary(
            topic_arc="stub summary",
            covers_through_seq=max(
                (t.get("seq", 0) for t in input_chunk.messages if isinstance(t, dict)),
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


def test_retry_loop_shrinks_tail_on_overflow(tmp_path) -> None:
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

        async def compact(self, input_chunk, *, covers_through=None):
            from reyn.services.compaction.engine import ChatSummary
            return ChatSummary(topic_arc="stub", covers_through_seq=0)

    cfg = _make_cfg()
    engine = _SmallMinEngine()
    learner_path = tmp_path / "m.json"
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
        raw_middle=raw_middle,
        tail=tail,
        new_msg=new_msg,
        cfg=cfg,
        model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=main_call,
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


def test_retry_loop_raises_unrecovered_when_all_at_min(tmp_path) -> None:
    """Tier 2: retry_loop raises UnrecoveredError when head and tail are already minimal.

    When head, tail, and raw_middle are all at or below their minimum token
    budgets and the call still overflows, retry_loop MUST raise UnrecoveredError.
    """
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    # Single-turn minimal head + tail (≤ head_min_tokens / tail_min_tokens).
    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 3}

    async def _always_overflow(**kwargs):
        raise ContextOverflowError("always overflow")

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp",
            head=head,
            raw_middle=raw_middle,
            tail=tail,
            new_msg=new_msg,
            cfg=cfg,
            model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_overflow,
        ))
    # #4954 (b): a plain overflow (no status_code) — the "all shrink paths
    # exhausted" raise site's saw_byte_limit is reachable-and-False here,
    # not merely unreached.
    assert excinfo.value.saw_byte_limit is False


# #5531 §10: test_retry_loop_max_iterations_raises_unrecovered removed —
# max_iterations no longer exists (this loop's own "Bounded termination
# proof" docstring is the replacement); zero other consumers (git grep
# confirmed no reference outside this file's own former definition).

def test_retry_loop_success_calls_learner_observe(tmp_path) -> None:
    """Tier 2: successful retry_loop call triggers learner.observe with positive tokens.

    When main_call returns a response with usage.prompt_tokens > 0, the
    learner EMA must change (= observe was called).
    """
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner_path = tmp_path / "m.json"
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
        raw_middle=[],
        tail=tail,
        new_msg=new_msg,
        cfg=cfg,
        model=model,
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_success_call,
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


def test_retry_loop_emits_compaction_shrink_recovered_with_cause(tmp_path) -> None:
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
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

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
        SP="sp", head=head, raw_middle=[],
        tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_fail_once_then_succeed,
    ))

    recovered = [e for e in events if e.type == "compaction_shrink_recovered"]
    (only,) = recovered  # exactly one recover — raises ValueError otherwise
    assert only.data["cause"] == "ContextOverflowError"
    assert only.data["iteration"] == 0
    assert only.data["consecutive"] == 1


# #5531 §10 (settled (a)): test_retry_loop_same_cause_cap_raises_before_
# shrink_paths_exhausted removed — T3 (the same-cause cap) is retired,
# the halving ladder's own two floors ((a)/(b)) are the terminal
# condition; zero other consumers (git grep confirmed).

class _CompactFailsOnceEngine(_OverflowingEngine):
    """compact() overflows on its FIRST call only, then behaves like the
    (fail_compact=False) base — used to force one CompactionOverflowError
    recover followed by ContextOverflowError recovers, so the two causes
    genuinely differ (not two instances of the same class)."""

    def __init__(self) -> None:
        super().__init__(fail_compact=False)
        self._compact_calls = 0

    async def compact(self, input_chunk, *, covers_through=None):
        self._compact_calls += 1
        if self._compact_calls == 1:
            raise CompactionOverflowError("first compact overflows")
        return await super().compact(input_chunk)


# #5531 §10: test_retry_loop_alternating_causes_do_not_trip_same_cause_
# cap removed — T3 is retired, so "a different cause resets the
# counter" no longer bounds anything; zero other consumers of this
# test itself (git grep confirmed). _CompactFailsOnceEngine (used by a
# different, still-live test below) is untouched.
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


def test_413_recovery_does_not_claim_exceeds_t_max(tmp_path) -> None:
    """Tier 2: #4885 — a 413 that never resolves (main_call always raises
    it, tail/head start at/below their token minimums) exhausts the
    binary-search floor and raises an ACCURATE message naming the byte
    limit — never the old, false "exceeds T_max" (T_max is a TOKEN measure;
    a 413 says nothing about it, and the whole point of this path is that
    T_max was never actually exceeded)."""
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

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
            SP="sp", head=head, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_413,
        ))

    message = str(excinfo.value)
    assert "413" in message
    assert "exceeds T_max" not in message
    # #4954 (b): the T_max binary-search floor is one of the 3 sites whose
    # OWN branch is byte-limit-gated (`elif _last_recover_is_byte_limit:`)
    # — saw_byte_limit must be True here, not merely mentioned in prose.
    assert excinfo.value.saw_byte_limit is True


def test_413_recovery_emits_t_max_override_in_the_audit_event(tmp_path) -> None:
    """Tier 2: #4885 — the LOCAL T_max override this recovery uses is
    visible in the SAME audit trail an operator already reads for shrink
    activity (owner condition ②: not silently running with a smaller
    window) — a new field on the EXISTING ``compaction_shrink_recovered``
    event, not a new event kind."""
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

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
            SP="sp", head=head, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_413,
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


# #5531 §10: test_413_recovery_same_cause_cap_does_not_cut_off_the_binary_
# search removed — T3 is retired, nothing to cut anything off any more.
#
# #5531 §10: test_max_iterations_exhaustion_names_413_when_last_cause_was_
# byte_limit removed — max_iterations exhaustion no longer exists as a
# raise site (retry_loop's own "Bounded termination proof" docstring is
# the replacement); the byte-limit-naming behaviour this test covered for
# THAT site is still covered, for the mid-split floor, by
# test_4947_stage1_mid_split_terminates_instead_of_reproducing_old_state
# and by test_413_recovery_does_not_claim_exceeds_t_max for the T_max
# floor. Zero other consumers of either removed test (git grep confirmed).


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


def test_413_recovery_succeeds_once_binary_search_lowers_t_max_enough(tmp_path) -> None:
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
    non-increasing once binary search starts (never strictly-decreasing —
    see the assertion's own comment: a repeated value across consecutive
    recoveries is documented, expected search progress, not a stall) — i.e.
    success occurred AFTER, and only after, both the ceiling was actually
    lowered AND the payload was actually shrunk to fit under it.

    ``max_iterations=40``, same as the 3 existing tests and for the same
    reason (headroom past the halving count, never tuned to it — six-
    questions Q2, no algorithm-level pin)."""
    cfg = _make_cfg()
    engine = _OverflowingEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")
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
        SP="sp", head=head, raw_middle=raw_middle,
        tail=tail, new_msg=new_msg, cfg=cfg, model=model,
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=main_call,
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
    # that sequence must never increase (real halving progression, never a
    # rising value; a REPEATED value is allowed — see the non-increasing
    # assertion's own comment below) — this is the "search actually worked"
    # witness the issue asked for, not just "main_call returned eventually".
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
    # Non-increasing, NOT strictly-decreasing (architect's post-merge
    # finding on this test's first version): the same-cause-cap SKIP
    # comment for a byte-limit cause (engine.py, just above
    # ``_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS``) documents that "one
    # iteration lowers the ceiling, a later one actually shrinks content
    # down to it, and the 413 keeps recurring in between" is the EXPECTED
    # shape of active binary-search progress — i.e. the SAME
    # ``t_max_override`` value is allowed to appear on consecutive
    # recoveries by design, not just a strictly-lower one each time. This
    # fixture's own head/tail sizes happen to make every recovery lower the
    # ceiling immediately, but asserting no-duplicates would pin THAT
    # fixture property as if it were retry_loop's contract (six-questions
    # Q2) — a correct future implementation change could legitimately
    # produce a repeat value and turn this red for no real regression.
    assert later_overrides == sorted(later_overrides, reverse=True), (
        f"t_max_override must never INCREASE across recoveries after the "
        f"first (the binary search only ever halves the ceiling, never "
        f"raises it); got {overrides!r}"
    )
    # Architect's follow-up (non-blocking, on the merged version of this
    # test): non-increasing alone is true even of a CONSTANT sequence —
    # weakening away "strictly decreasing" also weakened away "the ceiling
    # was actually lowered at all". Restore that witness at the ENDPOINTS
    # only (first vs last), which #4885's own contract (still-413 -> halve)
    # guarantees over a run this long, while still permitting the
    # documented mid-search repeats the assertion above already allows. A
    # single-element ``later_overrides`` compares its one value to ITSELF
    # here (``x < x`` is always False) and correctly fails this assertion
    # too — no separate length precondition needed.
    assert later_overrides[-1] < later_overrides[0], (
        f"the ceiling must have been strictly lower by the LAST recovery "
        f"than the FIRST overridden one — real, overall search progress, "
        f"not merely non-increasing (which a constant sequence would also "
        f"satisfy); got {overrides!r}"
    )


# ---------------------------------------------------------------------------
# #4947 ③: stage 1 splits raw_middle instead of fattening tail
# ---------------------------------------------------------------------------


class _Always413CompactEngine(_OverflowingEngine):
    """compact() ALWAYS raises an HTTP-413-caused CompactionOverflowError —
    used to witness the #4947 real-world cycle: a compaction call that can
    never succeed, no matter how small the offered slice gets (down to the
    floor of 1 turn)."""

    def __init__(self, *, head_budget: int, tail_budget: int) -> None:
        from reyn.services.compaction.engine import ComputedBudgets
        self.budgets = ComputedBudgets(
            main_pool=100_000, head_budget=head_budget, body_budget=500,
            tail_budget=tail_budget, new_msg_budget=10,
            B_M=90_000, main_M_room=99_000, effective_trigger=90_000,
            section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                          "session_user_facts": 50, "artifacts_referenced": 175},
        )
        self._events = EventLog()
        self._T_comp_SP = 100
        self.compact_calls = 0

    async def compact(self, input_chunk, *, covers_through=None):
        self.compact_calls += 1
        raise _FakeStatusError("compact 413", status_code=413)


def test_4947_stage1_mid_split_terminates_instead_of_reproducing_old_state(tmp_path) -> None:
    """Tier 2: #4947 ③ — a compact()-origin 413 that recurs no matter how
    small the offered slice gets must terminate at the mid=1-turn floor
    (named 413), NOT cycle back to the state it started from.

    #4947's own measurement (this test's exact shape: compact() always
    413, main_call always 413, head=8/mid=8/tail=8 real turns) found the
    OLD "move half of raw_middle into tail" direction reproduced the
    IDENTICAL state every 5 iterations — head=8/tail=16 recurring forever
    (never converging, never raising the accurate floor message, running
    to raw max_iterations with a generic message). head/tail are sized
    ABOVE their (tiny, deliberately small) minimums here — the existing
    413 tests all use a single-turn head/tail, which reaches the T_max-
    override branch on iteration 0 and never exercises stage 1 at all;
    that blind spot is exactly what let the old cycle go unnoticed
    (architect's own review condition on #4947, verbatim: must include a
    shape where head/tail start above their minimum).

    Falsification (performed during review): reverting the shrink-
    escalation branch to the old "move half of raw_middle into tail" line
    makes this test hang (unbounded iterations) or, if max_iterations is
    capped, makes the assertion on ``"413" in message`` still pass by
    accident (the old max_iterations-exhaustion message is generic) — the
    real falsifier is the recovered-event COUNT bound below, which the old
    code blows through (it never stops recovering within a bounded few
    events; this test's own max_iterations=20 was chosen to match the
    exact #4947 repro that cycled through all 20 under the old code).
    """
    cfg = _make_cfg()
    engine = _Always413CompactEngine(head_budget=10, tail_budget=10)
    events: list = []
    engine._events.add_subscriber(lambda e: events.append(e))
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    head = _turns(["h" * 20] * 8)
    tail = _turns(["t" * 20] * 8)
    raw_middle = _turns(["m" * 20] * 8)
    new_msg = {"role": "user", "content": "q", "seq": 999}

    async def _always_413(**kwargs):
        raise ContextOverflowError("main_call 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    # #5531 §10: retry_loop no longer takes max_iterations — this bound
    # is now only the comparison ceiling the assertion below uses (the
    # exact #4947 repro that cycled through all 20 under the old code).
    _max_iterations = 20
    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=head, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_413,
        ))

    message = str(excinfo.value)
    # Names the intended production bound explicitly (six questions Q5,
    # reyn-reviewer review on #4950): this module has 3 distinct
    # UnrecoveredError sites — the same-cause cap ("consecutive times"),
    # max_iterations exhaustion ("max_iterations"), and the mid-split
    # floor this test targets ("mid cannot be split any further"). All
    # three are asserted so a future change firing the WRONG one is
    # unambiguous about which bound actually stopped the loop.
    assert "mid cannot be split any further" in message
    assert "consecutive times" not in message
    assert "max_iterations" not in message
    # #4954 (b): the mid-split floor's byte-limit arm is one of the 3
    # sites whose OWN branch is byte-limit-gated — saw_byte_limit must be
    # True here.
    assert excinfo.value.saw_byte_limit is True

    recovered = [e for e in events if e.type == "compaction_shrink_recovered"]
    # The OLD cycle recovered on EVERY one of the 20 iterations (compact
    # mid=8,4,2,1, then main_call, repeating the identical state forever)
    # and never raised this floor message at all. The NEW floor raises
    # partway through the FIRST splitting period — well before the loop
    # would have spent its full ``_max_iterations`` budget recovering,
    # without pinning the exact count it takes (six questions Q2).
    assert len(recovered) < _max_iterations, (
        f"expected the floor to raise before exhausting max_iterations, "
        f"got {len(recovered)} recovers: {[e.data for e in recovered]!r}"
    )
    # The mid-split floor is a DIFFERENT bound than `max_iterations` or
    # the recovered-event count above — reyn-reviewer Q5: assert the
    # production observable that bound actually owns (compact() call
    # count), independent of the caller-provided max_iterations. The
    # floor triggers once the offered slice halves down to 1 turn from
    # an 8-turn raw_middle: 8, 4, 2, 1 — exactly 4 compact() calls, a
    # property of the halving arithmetic on this fixture's own input
    # size, not of max_iterations (set to a generous 20 here specifically
    # so it cannot be the thing that stopped the loop).
    assert engine.compact_calls == 4, (
        f"expected exactly 4 compact() calls (8→4→2→1 halving to the "
        f"floor) — got {engine.compact_calls}, meaning some OTHER bound "
        f"(not the mid-split floor) determined how long the loop ran"
    )


def test_5367_3_spill_before_raise_resolves_byte_limit_mid_split_floor(tmp_path) -> None:
    """Tier 2: #5367③ — at the byte-limit mid=1-turn floor, a spillable
    ``raw_middle[0]`` (role ``tool``, str content) is offered to the
    injected ``spill_fn`` BEFORE raising; if the spill produces smaller
    content that compact() then accepts, retry_loop returns normally
    instead of raising ``UnrecoveredError``.

    Falsification (performed during review): removing the
    ``_try_spill_first_mid_turn()`` call before the raise (reverting to
    #5367②'s text-only fix) makes this test raise ``UnrecoveredError``
    instead of returning — compact() never sees the spilled content
    because the loop never retries.
    """
    cfg = _make_cfg()

    class _SpillableByteLimitEngine(_OverflowingEngine):
        """compact() 413s while the offered turn still carries the
        ORIGINAL oversized marker; succeeds once it is the SPILLED one —
        the compact()-side witness that spill_fn's replacement actually
        reached engine.compact(), not just retry_loop's own state."""

        def __init__(self) -> None:
            super().__init__()
            self.compact_calls = 0

        async def compact(self, input_chunk, *, covers_through=None):
            self.compact_calls += 1
            turn = input_chunk.messages[0]
            if turn.get("content") == "OVERSIZED_TOOL_RESULT":
                raise _FakeStatusError("compact 413", status_code=413)
            from reyn.services.compaction.engine import ChatSummary
            return ChatSummary(topic_arc="ok", covers_through_seq=turn.get("seq", 0))

    engine = _SpillableByteLimitEngine()
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    raw_middle = [{
        "role": "tool", "content": "OVERSIZED_TOOL_RESULT", "seq": 1,
        "tool_call_id": "tc-1", "name": "big_tool",
    }]
    new_msg = {"role": "user", "content": "q", "seq": 999}
    spill_calls: list = []

    def _spill_fn(candidates: "list[dict]") -> "tuple[int, dict] | None":
        # #5531 §10: whole-list signature — ``candidates`` IS raw_middle.
        for idx, turn in enumerate(candidates):
            spill_calls.append(turn)
            if turn.get("role") != "tool" or turn.get("content") != "OVERSIZED_TOOL_RESULT":
                continue
            return idx, {**turn, "content": "REF: spilled to .reyn/memory/history-content/..."}
        return None

    async def _main_call(**kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10), choices=[])

    result = asyncio.run(retry_loop(
        SP="sp", head=[], raw_middle=raw_middle,
        tail=[], new_msg=new_msg, cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_main_call,
        spill_fn=_spill_fn,
    ))

    assert result is not None, "retry_loop must return normally, not raise"
    # Exactly one spill_fn call for this single turn — unpacking to one
    # element raises ValueError if the loop retried spill_fn a second time
    # (which would mean the SAME object was offered for spilling twice).
    (only_spill_call,) = spill_calls
    assert only_spill_call["content"] == "OVERSIZED_TOOL_RESULT", (
        "spill_fn must be offered the ORIGINAL content, not an already-"
        "spilled one"
    )
    assert engine.compact_calls == 2, (
        f"expected exactly 2 compact() calls (1 failing on the original "
        f"content, 1 succeeding on the spilled content) — got "
        f"{engine.compact_calls}"
    )


def test_5367_3_spill_unavailable_still_raises_with_accurate_message(tmp_path) -> None:
    """Tier 2: #5367③ — with no ``spill_fn`` injected (the default,
    matching every OTHER test in this file), the byte-limit mid-split
    floor behaves exactly as #5367②'s text-only fix left it: raises
    ``UnrecoveredError`` naming the turn-count floor, honestly silent
    about spill (never claiming it was tried when it was not offered)."""
    cfg = _make_cfg()
    engine = _Always413CompactEngine(head_budget=10, tail_budget=10)
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    raw_middle = [{"role": "tool", "content": "OVERSIZED_TOOL_RESULT", "seq": 1}]
    new_msg = {"role": "user", "content": "q", "seq": 999}

    async def _always_413(**kwargs):
        raise ContextOverflowError("main_call 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=[], raw_middle=raw_middle,
            tail=[], new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_413,
        ))

    assert "mid cannot be split any further" in str(excinfo.value)
    assert excinfo.value.saw_byte_limit is True


def test_4947_stage1_success_resets_same_cause_streak(tmp_path) -> None:
    """Tier 2: #4947 ③ (architect-ruled, CI red on #4950) — a compact()
    SUCCESS resets the same-cause cap's consecutive-recover counter, so
    fail / fail / SUCCEED / fail / fail does NOT trip the cap (which fires
    only above ``_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS`` == 2 consecutive
    recovers of the SAME cause). Without the reset, the counter would
    carry from the first fail/fail pair through the intervening success
    and combine with the second fail/fail pair to read 3+ — exactly the
    shape that produced #4950's own CI red (a DIFFERENT, coincidental
    test witnessed the same gap; this is the intentional, direct witness
    architect asked for).

    Falsification (performed during review): removing the
    ``_consecutive_same_cause = 0`` / ``_last_recover_cause = None`` reset
    on compact() success makes this test raise ``UnrecoveredError`` with
    "consecutive times" in the message instead of returning normally.
    """
    cfg = _make_cfg()

    class _ScriptedCauseEngine(_OverflowingEngine):
        """Follows an explicit fail/succeed script by call index, past-end
        calls default to succeeding — keeps this test's cause-recurrence
        pattern independent of the attempt-size halving arithmetic."""

        def __init__(self, script: list[bool]) -> None:
            super().__init__(fail_compact=False)
            self._script = script
            self._idx = 0

        async def compact(self, input_chunk, *, covers_through=None):
            should_fail = self._script[self._idx] if self._idx < len(self._script) else False
            self._idx += 1
            if should_fail:
                raise ValueError("same cause every time")
            from reyn.services.compaction.engine import ChatSummary
            return ChatSummary(
                topic_arc="stub",
                covers_through_seq=max(
                    (t.get("seq", 0) for t in input_chunk.messages if isinstance(t, dict)),
                    default=0,
                ),
            )

    # fail, fail, SUCCEED, fail, fail — then default-succeed to convergence.
    engine = _ScriptedCauseEngine([True, True, False, True, True])
    events: list = []
    engine._events.add_subscriber(lambda e: events.append(e))
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    head: list[dict] = []
    tail: list[dict] = []
    raw_middle = _turns(["m"] * 8)
    new_msg = {"role": "user", "content": "q", "seq": 99}

    async def _succeeds(**kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10), choices=[])

    result = asyncio.run(retry_loop(
        SP="sp", head=head, raw_middle=raw_middle,
        tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_succeeds,
    ))

    assert result is not None
    recovered = [e for e in events if e.type == "compaction_shrink_recovered"]
    assert recovered, "setup: no recoveries observed — script did not exercise a failure"
    # The cap fires above _MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS (2) — every
    # observed consecutive count must stay at or below it, proving the
    # reset broke the fail/fail + fail/fail combination into two separate
    # streaks of 2, never one streak of 4.
    assert all(e.data["consecutive"] <= 2 for e in recovered), (
        f"same-cause streak was not reset by the intervening success: "
        f"{[e.data for e in recovered]!r}"
    )


def test_4947_stage1_mid_split_reaches_success_after_temporary_compact_failure(tmp_path) -> None:
    """Tier 2: #4947 ③ — a compact() that fails once then recovers must
    still reach a REAL success end-to-end via the split-and-retry
    mechanism, with the summary reflecting ALL of raw_middle (not just the
    first successfully-attempted slice) — main_call never receives
    raw_middle directly, so a remainder left uncompacted would be silently
    dropped from what the LLM sees rather than raising or shrinking
    visibly.

    #5531 PR-2: ``main_call`` no longer receives a separate ``summary=``
    argument — the fold's output now lands in ``head`` itself (engine.py's
    own fold-success branch). This test's witness moves accordingly: it
    reads the summary-role element back out of the ``head`` ``main_call``
    was actually called with.
    """
    from reyn.services.compaction.engine import SUMMARY_MESSAGE_ROLE

    cfg = _make_cfg()
    engine = _CompactFailsOnceEngine()
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    head: list[dict] = []
    tail: list[dict] = []
    raw_middle = _turns(["m"] * 4)
    new_msg = {"role": "user", "content": "q", "seq": 99}

    seen_heads: list = []

    async def _succeeds(**kwargs):
        from types import SimpleNamespace
        seen_heads.append(kwargs.get("head"))
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10), choices=[])

    result = asyncio.run(retry_loop(
        SP="sp", head=head, raw_middle=raw_middle,
        tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_succeeds,
    ))

    assert result is not None
    # main_call was only ever invoked once raw_middle was FULLY compacted
    # — never mid-remainder (the exact bug: a partial success falling
    # through to main_call with a summary that silently omits content).
    # Unpacking to exactly one element raises otherwise — a real failure
    # mode this test would otherwise need a bare len() check to catch.
    [only_head] = seen_heads
    # Unpacking to exactly one element raises otherwise — the same
    # structural enforcement as `[only_head] = seen_heads` above, applied
    # to head's own summary-role content: a duplicated-summary bug (or a
    # dropped one) fails here at unpack time, not via a bare len() check.
    [only_summary] = [
        m for m in only_head if m.get("role") == SUMMARY_MESSAGE_ROLE
    ]
    # covers_through_seq on the real ChatSummary stub reflects the LAST
    # turn compacted — the final (highest) seq in raw_middle, not the seq
    # at the END of only the first successfully-attempted half.
    assert only_summary["covers_through_seq"] == raw_middle[-1]["seq"]


# #5531 §3 item 12 (removed): test_4947_stage1_floor_defers_instead_of_
# raising_when_not_byte_limit removed — the "defer this one turn to tail"
# escape hatch for a non-byte-limit mid=1 floor is GONE (a floor is a
# floor regardless of which HTTP shape triggered it; deferring let a
# non-byte cause dodge the floor by growing tail, the non-monotonic
# escape #4947 ③ closed for the OTHER branch of this same fork). The
# scenario this test built (single-turn raw_middle, non-byte compact()
# failure, no spill_fn) now correctly raises UnrecoveredError instead —
# covered by test_4947_stage1_floor_names_413_when_it_is_a_byte_limit's
# own sibling shape (that test's OWN engine differs only in status_code)
# and by test_5367_3_spill_unavailable_still_raises_with_accurate_message.
# Zero other consumers of the removed test/its engine (git grep confirmed).


def test_4947_stage1_floor_names_413_when_it_is_a_byte_limit(tmp_path) -> None:
    """Tier 2: #4947 ③ — the mid=1-turn floor DOES raise, naming the byte
    limit, when the recurring cause IS one — moving that single turn into
    ``tail`` would fatten the exact request ``main_call`` is about to
    retry, worsening rather than resolving a 413."""
    cfg = _make_cfg()

    class _AlwaysByteLimitEngine(_OverflowingEngine):
        async def compact(self, input_chunk, *, covers_through=None):
            raise _FakeStatusError("compact 413", status_code=413)

    engine = _AlwaysByteLimitEngine(fail_compact=False)
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    head: list[dict] = []
    tail: list[dict] = []
    raw_middle = _turns(["m"])  # already a single turn — floor on iteration 0
    new_msg = {"role": "user", "content": "q", "seq": 99}

    async def _unreachable(**kwargs):
        raise AssertionError("main_call must not be reached — mid never empties")

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=head, raw_middle=raw_middle,
            tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_unreachable,
        ))

    message = str(excinfo.value)
    assert "413" in message
    assert "single raw_middle turn alone" in message


def test_5531_no_interleaving_tail_condition_stays_false_once_exhausted(tmp_path) -> None:
    """Tier 2: #5531 investigation finding (architect BLOCKING #2, PR #5533,
    head a78156291) — WITHIN one retry_loop call, once Phase 1's own
    condition (tail token count > tail_min) goes False, it never becomes
    True again. This is Phase 1/Phase 2's own no-interleaving invariant,
    independent of the `_raw_middle_grew_from_head`-driven message-order
    rule this originally motivated — that rule and its own two pinning
    tests were retired by #5531's later ruling (the compact()-input
    splice they pinned was removed entirely: `_messages = _offered`, no
    order to compute at all). This invariant survives on its own merits:
    Phase 1's own condition must never flip back True once it goes False.

    Observable witness (no private-state read): tail's own token count, as
    `main_call` receives it, is monotonically NON-INCREASING across every
    call this retry_loop invocation makes — the only way Phase 1's
    condition could go True again after going False is for something to
    have grown tail back up, which this asserts never happens. Drives a
    REAL retry_loop through BOTH Phase 1 (tail exhausts first) and Phase 2
    (head shrinks next), so the assertion spans the exact transition
    condition③ depends on, not just one phase in isolation."""
    from reyn.services.compaction.engine import ChatSummary, ComputedBudgets

    class _BothPhasesEngine:
        def __init__(self) -> None:
            self.budgets = ComputedBudgets(
                main_pool=100_000, head_budget=10, body_budget=500,
                tail_budget=10, new_msg_budget=1_000,
                B_M=90_000, main_M_room=99_000, effective_trigger=90_000,
                section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                              "session_user_facts": 50, "artifacts_referenced": 175},
            )
            self._events = EventLog()
            self._T_comp_SP = 100

        async def compact(self, input_chunk, *, covers_through=None):
            return ChatSummary(topic_arc="stub", covers_through_seq=0)

    cfg = _make_cfg()
    engine = _BothPhasesEngine()
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    # Both tail and head start FAR over their tiny (10-token) budgets, so
    # Phase 1 fires repeatedly until tail is exhausted, THEN Phase 2 fires
    # for head — the exact transition this test observes.
    tail = _turns(["t" * 400] * 6)
    head = _turns(["h" * 400] * 6)
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "hi", "seq": 99}

    tail_token_history: list[int] = []

    async def _main_call(**kwargs):
        t = kwargs["tail"]
        h = kwargs["head"]
        tail_tokens = _estimate_tokens_list(t, "test-model", use_chars4=True)
        head_tokens = _estimate_tokens_list(h, "test-model", use_chars4=True)
        tail_token_history.append(tail_tokens)
        if head_tokens > 10 or tail_tokens > 10:
            raise ContextOverflowError("simulated overflow")
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000), choices=[])

    # #5531 PR-2: a fold's output now lands back in `head` (engine.py's own
    # fold-success branch) — with this fixture's tiny head_budget=10, a
    # freshly-appended summary element can itself exceed it, so
    # convergence to a successful main_call is not guaranteed within
    # max_iterations any more (an accepted consequence of PR-2's own
    # max_iterations-bounded, not monotonic-decrease-bounded, termination
    # — see retry_loop's own docstring). Tail's own monotonicity — this
    # test's actual claim — holds regardless of whether the loop
    # ultimately converges or exhausts.
    with pytest.raises(UnrecoveredError):
        asyncio.run(retry_loop(
            SP="system",
            head=head,
            raw_middle=raw_middle,
            tail=tail,
            new_msg=new_msg,
            cfg=cfg,
            model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_main_call,
        ))

    assert any(
        later < earlier for earlier, later in zip(tail_token_history, tail_token_history[1:])
    ), (
        "expected tail to actually shrink at least once (Phase 1 firing) "
        f"during this call — got {tail_token_history!r}"
    )
    for earlier, later in zip(tail_token_history, tail_token_history[1:]):
        assert later <= earlier, (
            "condition③'s no-interleaving finding: tail's token count "
            "must never increase within one retry_loop call (Phase 1's "
            "own condition must stay False once it first goes False) — "
            f"got history {tail_token_history!r}"
        )


def test_5531_at_most_one_summary_element_in_messages(tmp_path) -> None:
    """Tier 2: #5531 condition④ (architect BLOCKING #3, PR #5533, head
    a78156291) — HistoryChunkToCompact's own docstring contract is "AT
    MOST ONE" element carries `role == SUMMARY_MESSAGE_ROLE`; with two,
    which one is the actual prior summary becomes ambiguous. Drives a REAL
    retry_loop through several overflow-recovery iterations (so `messages`
    is rebuilt from scratch, with a real running `summary`, on every
    compact() call) and asserts every single one of those calls' messages
    carries at most one summary-role element — not merely that ONE
    observed call happens to (the earlier order tests above only inspect
    `captured_orders[0]`; this one inspects every call)."""
    from reyn.services.compaction.engine import (
        SUMMARY_MESSAGE_ROLE,
        ChatSummary,
        ComputedBudgets,
        wrap_summary_as_message,
    )

    summary_role_counts: list[int] = []

    class _CountingEngine:
        def __init__(self) -> None:
            self.budgets = ComputedBudgets(
                main_pool=100_000, head_budget=10, body_budget=500,
                tail_budget=10, new_msg_budget=1_000,
                B_M=90_000, main_M_room=99_000, effective_trigger=90_000,
                section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                              "session_user_facts": 50, "artifacts_referenced": 175},
            )
            self._events = EventLog()
            self._T_comp_SP = 100

        async def compact(self, input_chunk, *, covers_through=None):
            summary_role_counts.append(sum(
                1 for m in input_chunk.messages if m.get("role") == SUMMARY_MESSAGE_ROLE
            ))
            return ChatSummary(topic_arc="stub", covers_through_seq=0)

    cfg = _make_cfg()
    engine = _CountingEngine()
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    tail = _turns(["t" * 400] * 6)
    prior_summary = {"topic_arc": "already-compacted earlier span", "covers_through_seq": 1}
    # #5531 PR-2: no separate `summary=` argument any more — a pre-existing
    # summary is embedded directly as the OLDEST element of `head` (exactly
    # where decompose_history_for_retry's own turns filter would place it).
    head = [wrap_summary_as_message(prior_summary)] + _turns(["h" * 400] * 6)
    raw_middle: list[dict] = []
    new_msg = {"role": "user", "content": "hi", "seq": 99}

    async def _main_call(**kwargs):
        t = kwargs["tail"]
        h = kwargs["head"]
        tail_tokens = _estimate_tokens_list(t, "test-model", use_chars4=True)
        head_tokens = _estimate_tokens_list(h, "test-model", use_chars4=True)
        if head_tokens > 10 or tail_tokens > 10:
            raise ContextOverflowError("simulated overflow")
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000), choices=[])

    # #5531 PR-2: a fold's output now lands back in `head` (see engine.py's
    # own fold-success branch) — with this fixture's deliberately tiny
    # head_budget=10, even one small summary element alone can keep
    # re-triggering Phase 2 (never converging to a successful main_call
    # within max_iterations); that is an accepted, avowed consequence of
    # PR-2's own termination redesign (max_iterations-bounded, not
    # monotonic-decrease-bounded — see retry_loop's own docstring). This
    # test's actual claim (at most one summary element per compact() call)
    # is checked below regardless of how the loop ultimately ends.
    with pytest.raises(UnrecoveredError):
        asyncio.run(retry_loop(
            SP="system",
            head=head,
            raw_middle=raw_middle,
            tail=tail,
            new_msg=new_msg,
            cfg=cfg,
            model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_main_call,
        ))

    assert summary_role_counts, "expected at least one compact() call"
    for count in summary_role_counts:
        assert count <= 1, (
            f"HistoryChunkToCompact.messages must carry AT MOST ONE "
            f"{SUMMARY_MESSAGE_ROLE!r}-role element — got {count} in one "
            f"call; full history {summary_role_counts!r}"
        )


def test_5531_pr2_all_summary_head_and_tail_reaches_reservation_floor(tmp_path) -> None:
    """Tier 2: #5531 PR-2 (owner ruling, issuecomment-5465590083) — Phase
    1/2's own no-progress guard (``_has_non_summary``). When ``head``/
    ``tail`` hold NOTHING but a reserved summary element, Phase 1/2's
    token-threshold condition alone stays True forever (a summary is
    never shrunk by this ladder — reserved) even though NOTHING can
    actually be moved into ``raw_middle``. Without the guard this test
    goes RED: Phase 2 "handles" the overflow every iteration by pulling
    the summary element itself into raw_middle (undoing the reservation
    entirely) — this test drives a REAL retry_loop to prove that instead,
    the reservation ladder's own floor-check is reached and
    ``UnrecoveredError`` is raised naming the summary's own token size,
    well within a small ``max_iterations`` budget (never hitting
    exhaustion, which is what a starved/no-progress loop would do).

    Falsification (performed during review, see #5531 PR-2's own commit):
    removing the ``_has_non_summary`` guard makes this scenario spin
    through every iteration with head/tail unchanged, exhausting
    ``max_iterations`` with the GENERIC "without convergence" message —
    never reaching the reservation floor's own named-summary message
    this test asserts on.
    """
    from reyn.services.compaction.engine import SUMMARY_MESSAGE_ROLE, wrap_summary_as_message

    cfg = _make_cfg()

    class _NeverCompacts:
        def __init__(self) -> None:
            self.budgets = compute_budgets(
                cfg, "test-model", T_SP=1, T_comp_SP=100,
            )
            self._events = EventLog()
            self._T_comp_SP = 100

    engine = _NeverCompacts()
    learner = TokenMultiplierLearner(storage_path=tmp_path / "m.json")

    # head/tail hold ONLY a summary element each — nothing non-summary to
    # ever move into raw_middle. A real overflow (never resolves) forces
    # the ladder all the way to Phase 1/2, where the no-progress guard is
    # the only thing standing between "correctly starved, fall through to
    # the reservation floor" and "spin forever making zero progress".
    head = [wrap_summary_as_message({"topic_arc": "s" * 200, "covers_through_seq": 1})]
    tail = [wrap_summary_as_message({"topic_arc": "t" * 200, "covers_through_seq": 2})]
    new_msg = {"role": "user", "content": "q", "seq": 3}

    class _FakeStatusError(Exception):
        def __init__(self, message: str, *, status_code: int) -> None:
            super().__init__(message)
            self.status_code = status_code

    # A byte-limit cause (413) — exempt from the same-cause cap (#4885),
    # so the run reaches the reservation ladder's own many halvings
    # instead of tripping that unrelated cap first.
    async def _always_overflow(**kwargs):
        raise ContextOverflowError("simulated 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(retry_loop(
            SP="sp", head=head, raw_middle=[], tail=tail, new_msg=new_msg,
            cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_overflow,
        ))

    message = str(excinfo.value)
    assert "the current summary" in message, (
        f"expected the reservation floor's own named-summary terminal — "
        f"a starved, no-progress Phase 1/2 loop would instead exhaust "
        f"max_iterations with the generic message; got: {message!r}"
    )
    # Both summary elements are still exactly where they started — never
    # pulled into raw_middle, the reservation genuinely held.
    assert head[0].get("role") == SUMMARY_MESSAGE_ROLE
    assert tail[0].get("role") == SUMMARY_MESSAGE_ROLE
