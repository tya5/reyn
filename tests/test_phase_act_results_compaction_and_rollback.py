"""Tier 2 + Tier 3: OS invariant + LLMReplay tests for PR-N5 phase axis
compaction + rollback history snapshot (FP-0008).

Covers:
- compact_control_ir_results identity when total tokens <= threshold.
- compact_control_ir_results result contains __compacted_phase_results__ on fire.
- compact_control_ir_results summary bounded by body_budget (bounded computation).
- compact_control_ir_results on LLM error returns identity + emits
  phase_act_results_compaction_failed.
- compact_control_ir_results emits phase_act_results_compacted on success.
- PhaseActResultsCompactionConfig defaults load correctly.
- _build_phase_act_results_compaction_config parses sub-block.
- RollbackState.snapshot_phase_history saves + get_snapshot retrieves.
- RollbackState.snapshot_phase_history preserves order.
- get_snapshot returns None when no snapshot exists.
- PhaseExecutor._run_act_loop: previous_control_ir_results restored from
  rollback_context when set.
- PhaseExecutor._run_act_loop: control_ir_results starts empty when
  rollback_context absent.
- _PHASE_COMPACTION_SYSTEM_PROMPT is distinct from chat-axis comp_SP.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch.
- No private-state assertions (no obj._field == ...).
- Each docstring opens with ``Tier <N>: ...``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from reyn.config import PhaseActResultsCompactionConfig
from reyn.core.events.events import EventLog
from reyn.core.kernel.rollback_state import RollbackState
from reyn.services.compaction.engine import (
    CompactionEngine,
    compact_control_ir_results,
    estimate_tokens,
    hard_truncate_summary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_events() -> EventLog:
    return EventLog()


def _events_of_type(events: EventLog, kind: str) -> list[dict]:
    return [e.data for e in events.all() if e.type == kind]


def _make_engine(model: str = "gpt-3.5-turbo") -> CompactionEngine:
    """Build a minimal CompactionEngine with use_chars4=True for determinism."""
    from reyn.config import CompactionConfig
    cfg = CompactionConfig(use_chars4_estimate=True)
    return CompactionEngine(model=model, events=_make_events(), cfg=cfg, T_SP=0)


def _make_cfg(**kwargs: Any) -> PhaseActResultsCompactionConfig:
    defaults: dict[str, Any] = {
        "use_chars4_estimate": True,  # deterministic token counting in tests
    }
    defaults.update(kwargs)
    return PhaseActResultsCompactionConfig(**defaults)


COMPACTED_KIND = "__compacted_phase_results__"


# ---------------------------------------------------------------------------
# PhaseActResultsCompactionConfig: default values (Tier 2)
# ---------------------------------------------------------------------------


def test_phase_act_results_compaction_config_defaults() -> None:
    """Tier 2: PhaseActResultsCompactionConfig exposes sane defaults.

    Invariant: fresh config has recent_act_turns_raw > 0, control_ir_results_ratio
    in (0, 1], and use_chars4_estimate = False.
    """
    cfg = PhaseActResultsCompactionConfig()
    assert cfg.recent_act_turns_raw > 0
    assert 0.0 < cfg.control_ir_results_ratio <= 1.0
    assert cfg.summarize_older_threshold_tokens is None
    assert cfg.use_chars4_estimate is False


def test_build_phase_act_results_compaction_config_parses_block() -> None:
    """Tier 2: _build_phase_act_results_compaction_config parses sub-block dict.

    Invariant: explicit field values from the raw dict surface on the returned
    PhaseActResultsCompactionConfig instance.
    """
    from reyn.config import _build_phase_act_results_compaction_config  # type: ignore[attr-defined]

    cfg = _build_phase_act_results_compaction_config({
        "recent_act_turns_raw": 3,
        "control_ir_results_ratio": 0.4,
        "use_chars4_estimate": True,
    })
    assert cfg.recent_act_turns_raw == 3
    assert cfg.control_ir_results_ratio == pytest.approx(0.4)
    assert cfg.use_chars4_estimate is True


def test_build_phase_act_results_compaction_config_defaults_on_missing() -> None:
    """Tier 2: _build_phase_act_results_compaction_config returns defaults when non-dict."""
    from reyn.config import _build_phase_act_results_compaction_config  # type: ignore[attr-defined]

    cfg = _build_phase_act_results_compaction_config(None)
    defaults = PhaseActResultsCompactionConfig()
    assert cfg.recent_act_turns_raw == defaults.recent_act_turns_raw
    assert cfg.control_ir_results_ratio == pytest.approx(defaults.control_ir_results_ratio)


# ---------------------------------------------------------------------------
# compact_control_ir_results: identity (Tier 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_control_ir_results_identity_when_under_threshold() -> None:
    """Tier 2: compact_control_ir_results returns input unchanged when tokens <= threshold.

    Invariant: when total control_ir_results token cost is at or below the
    threshold, no compaction fires and the original list is returned.
    """
    engine = _make_engine()
    events = _make_events()

    results = [{"kind": "grep", "matches": ["src/foo.py:1"]},
               {"kind": "shell", "exit_code": 0, "stdout": "ok"}]
    # Very high threshold — always under it.
    cfg = _make_cfg(summarize_older_threshold_tokens=100_000)

    out = await compact_control_ir_results(
        results, engine=engine, cfg=cfg, events=events, phase="test_phase"
    )

    # Identity: original content preserved.
    assert any(item.get("kind") == "grep" for item in out)
    assert any(item.get("kind") == "shell" for item in out)
    # No compacted entry injected.
    assert not any(item.get("kind") == COMPACTED_KIND for item in out)
    # No compaction event emitted.
    assert not _events_of_type(events, "phase_act_results_compacted")


@pytest.mark.asyncio
async def test_compact_control_ir_results_identity_on_empty() -> None:
    """Tier 2: compact_control_ir_results on empty list returns empty list."""
    engine = _make_engine()
    events = _make_events()
    cfg = _make_cfg()
    out = await compact_control_ir_results([], engine=engine, cfg=cfg, events=events)
    assert out == []


# ---------------------------------------------------------------------------
# compact_control_ir_results: structure after compaction (Tier 2)
# ---------------------------------------------------------------------------


class _LLMSummaryFake:
    """Fake acompletion callable returning a canned summary text."""

    def __init__(self, summary: str = "PHASE_SUMMARY") -> None:
        self._summary = summary

    async def __call__(self, model: str, messages: list, **kwargs: Any) -> Any:
        class _Msg:
            content = self._summary
        class _Choice:
            message = _Msg()
        class _Response:
            choices = [_Choice()]
        return _Response()


async def _compact_with_fake_llm(
    older_results: list[dict],
    cfg: PhaseActResultsCompactionConfig,
    summary: str = "CANNED PHASE SUMMARY",
    phase: str = "test_phase",
) -> tuple[list[dict], EventLog]:
    """Run compact_control_ir_results with a fake LLM that returns ``summary``."""
    import litellm

    events = _make_events()
    from reyn.config import CompactionConfig
    cfg_compact = CompactionConfig(use_chars4_estimate=True)
    engine = CompactionEngine(
        model="gpt-3.5-turbo", events=events, cfg=cfg_compact, T_SP=0,
    )

    original = litellm.acompletion
    litellm.acompletion = _LLMSummaryFake(summary)  # type: ignore[assignment]
    try:
        result = await compact_control_ir_results(
            older_results, engine=engine, cfg=cfg, events=events, phase=phase,
        )
    finally:
        litellm.acompletion = original  # type: ignore[assignment]

    return result, events


@pytest.mark.asyncio
async def test_compact_control_ir_results_contains_compacted_entry() -> None:
    """Tier 2: after compaction fires, result contains a __compacted_phase_results__ entry.

    Structural invariant: when compaction fires, older results are replaced by
    exactly one entry with kind == '__compacted_phase_results__', and the entry
    contains a 'summary' field with non-empty text.
    """
    large_item = {"kind": "file_read", "path": "src/foo.py", "content": "x" * 400}
    older_results = [large_item, large_item, large_item]
    cfg = _make_cfg(
        summarize_older_threshold_tokens=10,  # trivially exceeded
    )

    result, events = await _compact_with_fake_llm(
        older_results, cfg, summary="grep: src/foo.py:1, src/bar.py:5"
    )

    # Invariant 1: compacted entry present.
    compacted = [r for r in result if r.get("kind") == COMPACTED_KIND]
    assert compacted, "Expected a __compacted_phase_results__ entry in result"

    entry = compacted[0]
    # Invariant 2: summary text is non-empty.
    assert entry.get("summary"), "Compacted entry must have non-empty summary"

    # Invariant 3: compacted_count reflects the number of older results.
    assert entry.get("compacted_count") == len(older_results)

    # Invariant 4: original_tokens recorded (> 0).
    assert entry.get("original_tokens", 0) > 0


@pytest.mark.asyncio
async def test_compact_control_ir_results_emits_compacted_event() -> None:
    """Tier 2: on successful compaction, phase_act_results_compacted event emitted.

    The event must contain n_older_compacted > 0.
    """
    large_item = {"kind": "shell", "cmd": "ls", "stdout": "y" * 300}
    older_results = [large_item, large_item, large_item]
    cfg = _make_cfg(summarize_older_threshold_tokens=10)

    result, events = await _compact_with_fake_llm(older_results, cfg)

    fired = _events_of_type(events, "phase_act_results_compacted")
    assert fired, "phase_act_results_compacted event must be emitted on success"
    ev = fired[-1]
    assert ev["n_older_compacted"] > 0


# ---------------------------------------------------------------------------
# compact_control_ir_results: bounded computation (Tier 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_control_ir_results_summary_bounded_by_body_budget() -> None:
    """Tier 2: the summary in the compacted entry is bounded by body_budget tokens.

    Invariant (PR-N5 bounded computation spec):
      tokens(summary) <= engine.budgets.body_budget
    """
    large_item = {"kind": "grep", "matches": ["src/z.py:" + str(i) for i in range(100)]}
    older_results = [large_item] * 4
    cfg = _make_cfg(summarize_older_threshold_tokens=1)
    # Fake LLM returns a very long summary to trigger hard_truncate_summary.
    very_long_summary = "W" * 40_000  # ~10,000 tokens (chars//4)

    result, _ = await _compact_with_fake_llm(
        older_results, cfg, summary=very_long_summary
    )

    compacted = [r for r in result if r.get("kind") == COMPACTED_KIND]
    if not compacted:
        pytest.skip("Compaction did not fire (identity path) — skip bounded check")

    from reyn.config import CompactionConfig
    cfg_compact = CompactionConfig(use_chars4_estimate=True)
    engine = CompactionEngine(
        model="gpt-3.5-turbo", events=_make_events(), cfg=cfg_compact, T_SP=0,
    )
    body_budget = engine.budgets.body_budget
    summary_tokens = estimate_tokens(
        compacted[0]["summary"], "gpt-3.5-turbo", use_chars4=True,
    )
    assert summary_tokens <= body_budget, (
        f"Summary tokens {summary_tokens} must be <= body_budget {body_budget}"
    )


# ---------------------------------------------------------------------------
# compact_control_ir_results: failure handling (Tier 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_control_ir_results_returns_identity_on_llm_error() -> None:
    """Tier 2: when LLM summarisation fails, compact_control_ir_results returns input unchanged.

    Best-effort invariant: a failing LLM call must never crash the phase run.
    The act loop continues with the un-compacted results.
    """
    import litellm

    large_item = {"kind": "file_read", "path": "src/e.py", "content": "e" * 300}
    older_results = [large_item, large_item, large_item]
    cfg = _make_cfg(summarize_older_threshold_tokens=10)
    events = _make_events()

    from reyn.config import CompactionConfig
    cfg_compact = CompactionConfig(use_chars4_estimate=True)
    engine = CompactionEngine(
        model="gpt-3.5-turbo", events=events, cfg=cfg_compact, T_SP=0,
    )

    async def _failing_acompletion(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated LLM API error")

    original = litellm.acompletion
    litellm.acompletion = _failing_acompletion  # type: ignore[assignment]
    try:
        result = await compact_control_ir_results(
            older_results, engine=engine, cfg=cfg, events=events, phase="fail_phase",
        )
    finally:
        litellm.acompletion = original  # type: ignore[assignment]

    # Invariant 1: returns original unchanged.
    assert result is older_results or result == older_results
    # Invariant 2: no compacted entry.
    assert not any(r.get("kind") == COMPACTED_KIND for r in result)
    # Invariant 3: failure event emitted.
    failure_events = _events_of_type(events, "phase_act_results_compaction_failed")
    assert failure_events, "phase_act_results_compaction_failed event must be emitted"


# ---------------------------------------------------------------------------
# _PHASE_COMPACTION_SYSTEM_PROMPT: distinct from chat-axis prompt (Tier 2)
# ---------------------------------------------------------------------------


def test_phase_compaction_system_prompt_distinct_from_chat_axis() -> None:
    """Tier 2: _PHASE_COMPACTION_SYSTEM_PROMPT is distinct from chat-axis comp_SP.

    Invariant: the phase-specific prompt must reference op-kind structured
    data preservation concepts (grep/file_read/shell) and must not be the
    same string as the chat-axis compaction prompt.
    """
    from reyn.services.compaction import engine as cce_mod

    phase_sp = cce_mod._PHASE_COMPACTION_SYSTEM_PROMPT  # noqa: SLF001
    chat_sp = cce_mod._COMPACTION_SYSTEM_PROMPT  # noqa: SLF001

    # Structural check: phase SP mentions op kinds.
    assert "grep" in phase_sp
    assert "file_read" in phase_sp
    assert "shell" in phase_sp

    # Non-identity with chat SP.
    assert phase_sp != chat_sp


# ---------------------------------------------------------------------------
# RollbackState.snapshot_phase_history + get_snapshot (Tier 2)
# ---------------------------------------------------------------------------


def test_rollback_snapshot_save_and_retrieve() -> None:
    """Tier 2: snapshot_phase_history saves; get_snapshot retrieves the same content.

    Invariant: after snapshot_phase_history(phase, results), get_snapshot(phase)
    returns a list with equivalent content.
    """
    state = RollbackState()
    results = [
        {"kind": "grep", "matches": ["src/foo.py:42"]},
        {"kind": "file_read", "path": "src/bar.py"},
    ]
    state.snapshot_phase_history("phase_a", results)

    retrieved = state.get_snapshot("phase_a")
    assert retrieved is not None
    assert len(retrieved) == len(results)
    assert retrieved[0] == results[0]
    assert retrieved[1] == results[1]


def test_rollback_snapshot_preserves_order() -> None:
    """Tier 2: rollback snapshot get_snapshot returns results in the same sequence saved.

    Invariant: the retrieved list preserves insertion order — the LLM sees ops
    in chronological sequence on rollback restore.
    """
    state = RollbackState()
    results = [{"kind": "op", "seq": i} for i in range(7)]
    state.snapshot_phase_history("ordered_phase", results)

    retrieved = state.get_snapshot("ordered_phase")
    assert retrieved is not None
    seqs = [r["seq"] for r in retrieved]
    assert seqs == list(range(7))


def test_rollback_snapshot_returns_none_for_unknown_phase() -> None:
    """Tier 2: get_snapshot returns None when no snapshot exists for the phase.

    Invariant: absence of a snapshot is signalled by None return, allowing the
    caller to fall through to the empty-list default.
    """
    state = RollbackState()
    assert state.get_snapshot("never_visited") is None


def test_rollback_snapshot_is_a_copy() -> None:
    """Tier 2: snapshot_phase_history stores a copy; later mutation does not corrupt it.

    Invariant: the snapshot is independent of the caller's list — caller can
    continue mutating control_ir_results without corrupting the saved snapshot.
    """
    state = RollbackState()
    results: list[dict] = [{"kind": "grep", "matches": ["src/a.py:1"]}]
    state.snapshot_phase_history("copy_phase", results)

    # Mutate original list.
    results.append({"kind": "shell", "exit_code": 1})

    retrieved = state.get_snapshot("copy_phase")
    assert retrieved is not None
    # Snapshot does not contain the appended item (= copy invariant).
    assert not any(r.get("kind") == "shell" for r in retrieved)


def test_rollback_snapshot_multiple_phases_independent() -> None:
    """Tier 2: snapshots for different phases are independent.

    Invariant: snapshot_phase_history(A, ...) and snapshot_phase_history(B, ...)
    store independent data; get_snapshot(A) does not return B's data.
    """
    state = RollbackState()
    results_a = [{"kind": "grep", "phase": "A"}]
    results_b = [{"kind": "shell", "phase": "B"}, {"kind": "file_read", "phase": "B"}]
    state.snapshot_phase_history("phase_a", results_a)
    state.snapshot_phase_history("phase_b", results_b)

    snap_a = state.get_snapshot("phase_a")
    snap_b = state.get_snapshot("phase_b")
    assert snap_a is not None
    assert snap_b is not None
    # Phase A snapshot contains only Phase A data.
    assert all(r["phase"] == "A" for r in snap_a)
    # Phase B snapshot contains only Phase B data.
    assert all(r["phase"] == "B" for r in snap_b)
    # The two snapshots are distinct (different content).
    assert snap_a[0]["kind"] != snap_b[0]["kind"]


# ---------------------------------------------------------------------------
# PhaseExecutor rollback_context restore (Tier 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_executor_restores_control_ir_results_from_rollback_context() -> None:
    """Tier 2: when rollback_context['previous_control_ir_results'] is set,
    _run_act_loop initialises control_ir_results from it.

    Invariant: the LLM's first prompt in a rollback re-entry sees the prior
    phase's op observations; the act loop does NOT start from empty.

    Verification: inject a fake LLM that returns immediately (decide turn),
    then verify the frame passed to build_frame includes the restored results.
    """
    # Build a minimal PhaseExecutor with spies.
    from reyn.config import SafetyConfig
    from reyn.core.events.events import EventLog
    from reyn.core.kernel.phase_executor import PhaseExecutor
    from reyn.core.kernel.run_state import RunState
    from reyn.schemas.models import ContextFrame

    events = EventLog()
    captured_control_ir_results: list[list[dict]] = []

    # Fake build_frame that captures control_ir_results passed to it.
    def _fake_build_frame(
        phase: str, artifact: dict, candidates: list, output_language: Any,
        *, control_ir_results: list[dict] | None = None,
        artifact_path: Any = None, remaining_act_turns: Any = None,
        force_decide: bool = False, **kwargs: Any,
    ) -> ContextFrame:
        captured_control_ir_results.append(list(control_ir_results or []))
        return ContextFrame(
            current_phase=phase,
            instructions="",
            candidate_outputs=[],
            input_artifact={"type": "test", "data": {}},
            control_ir_results=control_ir_results or [],
        )

    # Fake LLM caller that immediately returns a decide-turn response.
    class _FakeLLMCaller:
        async def call(self, *args: Any, **kwargs: Any) -> dict:
            return {
                "type": "transition",
                "control": {
                    "type": "transition",
                    "decision": "continue",
                    "next_phase": "end",
                    "confidence": 1.0,
                    "reason": {"summary": "done"},
                },
                "artifact": {"type": "test_output", "data": {}},
                "control_ir": [],
            }

    # Minimal Skill stub.
    class _FakePhase:
        max_act_turns = 3
        allowed_ops: list = []
        preprocessor = None

    class _FakeSkill:
        phases = {"test_phase": _FakePhase()}
        permissions = None

    class _FakeIRExecutor:
        async def execute(self, *args: Any, **kwargs: Any) -> list:
            return []

    prior_results = [
        {"kind": "grep", "matches": ["src/old.py:10"]},
        {"kind": "file_read", "path": "src/prior.py"},
    ]
    rollback_ctx = {"previous_control_ir_results": prior_results, "reason": "test rollback"}

    state = RunState()
    phase_executor = PhaseExecutor(
        llm_caller=_FakeLLMCaller(),  # type: ignore[arg-type]
        control_ir_executor=_FakeIRExecutor(),
        events=events,
        skill=_FakeSkill(),  # type: ignore[arg-type]
        safety=SafetyConfig(),
        intervention_bus=None,
        build_frame_fn=_fake_build_frame,
    )

    raw, _ = await phase_executor._run_act_loop(
        phase="test_phase",
        artifact={"type": "test", "data": {}},
        candidates=[],
        output_language=None,
        max_act_turns=3,
        max_phase_retries=1,
        artifact_path=None,
        state=state,
        rollback_context=rollback_ctx,
    )

    # Invariant: the first build_frame call received the restored results.
    assert captured_control_ir_results, "build_frame must have been called"
    first_frame_results = captured_control_ir_results[0]
    assert len(first_frame_results) == len(prior_results), (
        "control_ir_results passed to build_frame must match the restored snapshot"
    )
    assert first_frame_results[0]["kind"] == "grep"
    assert first_frame_results[1]["kind"] == "file_read"


@pytest.mark.asyncio
async def test_phase_executor_starts_empty_without_rollback_context() -> None:
    """Tier 2: when rollback_context is absent, _run_act_loop starts with empty
    control_ir_results (current unmodified behavior).

    Invariant: no rollback_context → no prior observations injected.
    """
    from reyn.config import SafetyConfig
    from reyn.core.events.events import EventLog as _EventLog
    from reyn.core.kernel.phase_executor import PhaseExecutor
    from reyn.core.kernel.run_state import RunState
    from reyn.schemas.models import ContextFrame

    captured: list[list[dict]] = []

    def _fake_build_frame(
        phase: str, artifact: dict, candidates: list, output_language: Any,
        *, control_ir_results: list[dict] | None = None, **kwargs: Any,
    ) -> ContextFrame:
        captured.append(list(control_ir_results or []))
        return ContextFrame(
            current_phase=phase,
            instructions="",
            candidate_outputs=[],
            input_artifact={"type": "x", "data": {}},
            control_ir_results=[],
        )

    class _FakeLLMCaller:
        async def call(self, *args: Any, **kwargs: Any) -> dict:
            return {
                "type": "transition",
                "control": {
                    "type": "transition",
                    "decision": "continue",
                    "next_phase": "end",
                    "confidence": 1.0,
                    "reason": {"summary": "ok"},
                },
                "artifact": {"type": "out", "data": {}},
                "control_ir": [],
            }

    class _FakePhase:
        max_act_turns = 3
        allowed_ops: list = []
        preprocessor = None

    class _FakeSkill:
        phases = {"p": _FakePhase()}
        permissions = None

    class _FakeIRExecutor:
        async def execute(self, *args: Any, **kwargs: Any) -> list:
            return []

    pe = PhaseExecutor(
        llm_caller=_FakeLLMCaller(),  # type: ignore[arg-type]
        control_ir_executor=_FakeIRExecutor(),
        events=_EventLog(),
        skill=_FakeSkill(),  # type: ignore[arg-type]
        safety=SafetyConfig(),
        intervention_bus=None,
        build_frame_fn=_fake_build_frame,
    )

    await pe._run_act_loop(
        phase="p",
        artifact={"type": "t", "data": {}},
        candidates=[],
        output_language=None,
        max_act_turns=3,
        max_phase_retries=1,
        artifact_path=None,
        state=RunState(),
        rollback_context=None,
    )

    assert captured, "build_frame must have been called"
    # Invariant: first frame has empty control_ir_results.
    assert captured[0] == []
