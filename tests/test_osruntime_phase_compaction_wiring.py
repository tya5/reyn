"""Tier 2 + Tier 3: OS invariant + integration tests for PR-N8 phase compaction
engine wiring through OSRuntime.

Background (PR-N8, issue #1043):
  PR-N5 built the phase axis compaction mechanism
  (PhaseExecutor._run_act_loop → compact_control_ir_results).  PR-N8 wires the
  engine + cfg into OSRuntime so the mechanism fires in production.  Before this
  PR, ``grep -rn "phase_compaction" src/reyn/`` returned 0 hits in runtime.py —
  the engine was always None, the fire condition was always False, context kept
  growing unbounded.

Covers:
- Default construction: OSRuntime without injection constructs a non-None
  engine + cfg (lazy default construction).
- Injection invariant: explicitly passed engine/cfg are the exact objects
  threaded through to the public accessors.
- Production integration (Tier 3): OSRuntime.run() accumulates act-turn
  control_ir_results past recent_act_turns_raw; verifies
  phase_act_results_compacted event fires end-to-end.
- No-fire guard (Tier 2): short act loop (< recent_act_turns_raw) does NOT
  emit phase_act_results_compacted.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch.
- No private-state assertions (no obj._field == ...).
- litellm.acompletion replaced via direct callable assignment (= the same
  pattern used in test_phase_act_results_compaction_and_rollback.py — NOT
  MagicMock; it is a direct attribute replace of the module-level callable).
- Each docstring opens with ``Tier <N>: ...``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from reyn.config import CompactionConfig, PhaseActResultsCompactionConfig
from reyn.core.events.events import EventLog
from reyn.core.kernel.phase_executor import PhaseExecutor
from reyn.core.kernel.runtime import OSRuntime
from reyn.schemas.models import (
    ControlDecision,
    ControlReason,
    Phase,
    Skill,
    SkillGraph,
)
from reyn.services.compaction.engine import CompactionEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(
    *,
    name: str = "compaction_test_skill",
    phase_name: str = "main",
    max_act_turns: int = 20,
) -> Skill:
    """Build a minimal 1-phase skill that allows act turns and finishing."""
    phase = Phase(
        name=phase_name,
        instructions="execute some ops then finish",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
        max_act_turns=max_act_turns,
    )
    return Skill(
        name=name,
        entry_phase=phase_name,
        phases={phase_name: phase},
        graph=SkillGraph(
            transitions={},
            can_finish_phases=[phase_name],
        ),
        final_output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        final_output_name="result",
    )


def _make_engine(events: EventLog | None = None) -> CompactionEngine:
    """Build a minimal CompactionEngine with use_chars4=True for determinism."""
    cfg = CompactionConfig(use_chars4_estimate=True)
    return CompactionEngine(
        model="gpt-3.5-turbo",
        events=events or EventLog(),
        cfg=cfg,
        T_SP=0,
    )


def _make_compact_cfg(recent_act_turns_raw: int = 2) -> PhaseActResultsCompactionConfig:
    """Build a PhaseActResultsCompactionConfig that fires eagerly in tests."""
    return PhaseActResultsCompactionConfig(
        recent_act_turns_raw=recent_act_turns_raw,
        summarize_older_threshold_tokens=1,  # trivially exceeded → always compact
        use_chars4_estimate=True,
    )


def _events_of_type(events: EventLog, kind: str) -> list[dict]:
    return [e.data for e in events.all() if e.type == kind]


# ---------------------------------------------------------------------------
# _FakeLLMResponse: fake litellm response object (dict in, object out)
# ---------------------------------------------------------------------------


class _FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMsg(content)
        self.finish_reason = "stop"


class _FakeLitellmResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = None

    def model_dump(self) -> dict:
        return {
            "choices": [{"message": {"content": self.choices[0].message.content}}],
            "usage": None,
        }


# ---------------------------------------------------------------------------
# _ScriptedLLMAcompletion: fake litellm.acompletion callable
#
# Serves N act-turn responses, then 1 finish response.  Compaction calls
# (which also go through litellm.acompletion) are answered with a canned
# summary string — the compaction context prompt is a plain text summarisation
# request, so returning a non-JSON string is the correct shape.
#
# Identification heuristic: the compaction call has a system prompt containing
# "control_ir" or "op_kind" or "phase_act", or a user message that looks like
# a JSON list (older_results serialised as JSON).  In practice, the compaction
# system prompt is _PHASE_COMPACTION_SYSTEM_PROMPT which contains "grep" and
# "file_read" — distinguishable from the main LLM system prompt.
# ---------------------------------------------------------------------------


class _ScriptedLLMAcompletion:
    """Callable (not mock) that replaces litellm.acompletion in integration tests.

    Act turns: returns ``{"type": "act", "ops": []}`` up to ``n_act_turns``
    calls.  Decide turn: returns a valid finish response.
    Compaction call: detected by message count / system content, returns a
    canned plain-text summary (not JSON — compact_control_ir_results expects
    raw text back from the LLM, not JSON).
    """

    def __init__(
        self,
        finish_artifact_type: str = "result",
        n_act_turns: int = 3,
        fake_ir_result: dict | None = None,
    ) -> None:
        self._n_act_turns = n_act_turns
        self._finish_artifact_type = finish_artifact_type
        self._fake_ir_result = fake_ir_result or {
            "kind": "grep",
            "matches": ["src/main.py:10", "src/main.py:20"],
        }
        self._act_call_count = 0
        self._compaction_calls: int = 0

    def _is_compaction_call(self, messages: list[dict]) -> bool:
        """Heuristic: compaction calls use _PHASE_COMPACTION_SYSTEM_PROMPT."""
        if not messages:
            return False
        sys_content = ""
        for m in messages:
            if m.get("role") == "system":
                sys_content = m.get("content", "") or ""
                break
        # The phase compaction SP mentions "grep" and "file_read".
        return "grep" in sys_content and "file_read" in sys_content

    async def __call__(
        self,
        model: str,
        messages: list[dict],
        **kwargs: Any,
    ) -> _FakeLitellmResponse:
        if self._is_compaction_call(messages):
            self._compaction_calls += 1
            summary = "PHASE_COMPACT: grep: src/main.py:10, src/main.py:20"
            return _FakeLitellmResponse(summary)

        # Normal LLM call (act or decide).
        if self._act_call_count < self._n_act_turns:
            self._act_call_count += 1
            act_response = {"type": "act", "ops": []}
            return _FakeLitellmResponse(json.dumps(act_response))

        # Decide turn: return a finish.
        finish_response = {
            "control": {
                "type": "finish",
                "decision": "finish",
                "next_phase": None,
                "confidence": 1.0,
                "reason": {"summary": "done"},
            },
            "artifact": {
                "type": self._finish_artifact_type,
                "data": {"ok": True},
            },
            "control_ir": [],
        }
        return _FakeLitellmResponse(json.dumps(finish_response))


# ---------------------------------------------------------------------------
# _PhaseCompactionRuntime: OSRuntime with a fake IR executor injected.
#
# The fake IR executor returns a non-empty synthetic result per call so that
# control_ir_results grows across act turns — satisfying the
# len(control_ir_results) > recent_act_turns_raw fire condition without
# requiring a real filesystem.
#
# Design: we override _phase_executor after super().__init__() using a
# fresh PhaseExecutor constructed with the same settings as the parent's.
# This keeps the real PhaseExecutor code path (= the wiring under test) while
# swapping only the ControlIRExecutor.
#
# The attrs accessed from the parent are ONLY the public/wiring attrs used in
# OSRuntime.__init__'s own PhaseExecutor construction:
#   self.events, self.skill, self._llm_caller, self.control_ir_executor,
#   self._safety, self._intervention_bus, self.run_id, self.strict,
#   self.build_frame, self._phase_compaction_engine, self._phase_compaction_cfg.
# All of these are the same as what the parent already passes to PhaseExecutor.
# ---------------------------------------------------------------------------


class _FakeIRExecutor:
    """Stub ControlIRExecutor that returns one large synthetic result per call.

    Enables control_ir_results to grow across act turns without real filesystem
    ops.  Compliant with the no-MagicMock testing policy.
    """

    async def execute(
        self,
        ops: Any,
        *,
        phase: str = "",
        decl: Any = None,
        allowed_ops: Any = None,
        default_sandbox_policy: Any = None,
        compact_now: Any = None,  # #1176 B1: mirror the real execute() signature
    ) -> list[dict]:
        """Return one synthetic grep result regardless of the ops list."""
        return [
            {
                "kind": "grep",
                "matches": [f"src/a.py:{i}" for i in range(5)],
                "query": "test_pattern",
            }
        ]


class _PhaseCompactionRuntime(OSRuntime):
    """OSRuntime variant that uses _FakeIRExecutor so act turns produce results.

    After super().__init__() the parent has already constructed _phase_executor
    with the real ControlIRExecutor.  We replace _phase_executor with one that
    uses _FakeIRExecutor so control_ir_results accumulates — the compaction
    wiring (engine + cfg wired by PR-N8) is exercised end-to-end.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Reconstruct PhaseExecutor with the fake IR executor.  All other
        # dependencies are taken directly from the parent's init results.
        self._phase_executor = PhaseExecutor(
            llm_caller=self._llm_caller,
            control_ir_executor=_FakeIRExecutor(),
            events=self.events,
            skill=self.skill,
            safety=self._safety,
            intervention_bus=self._intervention_bus,
            run_id=self.run_id,
            strict=self.strict,
            build_frame_fn=self.build_frame,
            phase_compaction_engine=self._phase_compaction_engine,
            phase_compaction_cfg=self._phase_compaction_cfg,
        )


# ---------------------------------------------------------------------------
# Tier 2: default construction builds non-None engine + cfg
# ---------------------------------------------------------------------------


def test_osruntime_default_construction_builds_engine_and_cfg() -> None:
    """Tier 2: OSRuntime constructed without explicit engine/cfg has non-None defaults.

    Invariant (PR-N8 lazy construction): when no phase_compaction_engine or
    phase_compaction_cfg is passed, OSRuntime constructs them lazily.  Both
    public accessors must return non-None after __init__.
    """
    skill = _make_skill()
    runtime = OSRuntime(skill, model="gpt-3.5-turbo", run_id="wiring-test-default")

    assert runtime.phase_compaction_engine is not None, (
        "phase_compaction_engine must be non-None after OSRuntime default construction"
    )
    assert runtime.phase_compaction_cfg is not None, (
        "phase_compaction_cfg must be non-None after OSRuntime default construction"
    )


def test_osruntime_default_cfg_has_sane_defaults() -> None:
    """Tier 2: OSRuntime default-constructed cfg exposes recent_act_turns_raw > 0.

    Invariant: the lazily constructed cfg must use the PhaseActResultsCompactionConfig
    defaults (recent_act_turns_raw=5, control_ir_results_ratio in (0, 1]).
    """
    skill = _make_skill()
    runtime = OSRuntime(skill, model="gpt-3.5-turbo", run_id="wiring-test-cfg")

    cfg = runtime.phase_compaction_cfg
    assert cfg is not None
    assert cfg.recent_act_turns_raw > 0
    assert 0.0 < cfg.control_ir_results_ratio <= 1.0


# ---------------------------------------------------------------------------
# Tier 2: explicit injection passes through to public accessors
# ---------------------------------------------------------------------------


def test_osruntime_injection_engine_and_cfg_accessible() -> None:
    """Tier 2: explicitly passed engine + cfg are accessible via public properties.

    Invariant (injection path): when phase_compaction_engine + phase_compaction_cfg
    are passed to OSRuntime(), the public accessors return the exact same objects.
    This verifies that PR-N8 wiring stores the injected values, not a re-constructed
    default.
    """
    skill = _make_skill()
    events = EventLog()
    engine = _make_engine(events)
    cfg = _make_compact_cfg()

    runtime = OSRuntime(
        skill,
        model="gpt-3.5-turbo",
        run_id="wiring-test-inject",
        phase_compaction_engine=engine,
        phase_compaction_cfg=cfg,
    )

    assert runtime.phase_compaction_engine is engine, (
        "phase_compaction_engine accessor must return the injected engine instance"
    )
    assert runtime.phase_compaction_cfg is cfg, (
        "phase_compaction_cfg accessor must return the injected cfg instance"
    )


# ---------------------------------------------------------------------------
# Tier 3: production integration test — phase_act_results_compacted fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_osruntime_phase_compaction_fires_end_to_end() -> None:
    """Tier 3: OSRuntime.run() emits phase_act_results_compacted when act results accumulate.

    Production integration test: drives the full OSRuntime → PhaseExecutor →
    compact_control_ir_results pipeline.

    Setup:
    - recent_act_turns_raw=2, summarize_older_threshold_tokens=1 (trivially exceeded)
    - 4 scripted act turns → control_ir_results accumulates 4 entries
    - len(4) > recent_act_turns_raw(2) → compaction fires on act turn 3
    - litellm.acompletion replaced with _ScriptedLLMAcompletion (no MagicMock)
    - _PhaseCompactionRuntime: _FakeIRExecutor returns 1 result per act turn

    Verification: phase_act_results_compacted event in events.all() → wiring
    is end-to-end correct (engine + cfg reached PhaseExecutor via OSRuntime).
    """
    import litellm

    skill = _make_skill(max_act_turns=10)
    compact_cfg = _make_compact_cfg(recent_act_turns_raw=2)
    # Engine shared with the runtime so its events are captured on the same log.
    scripted_llm = _ScriptedLLMAcompletion(
        finish_artifact_type="result",
        n_act_turns=4,
    )

    original_acompletion = litellm.acompletion
    litellm.acompletion = scripted_llm  # type: ignore[assignment]
    try:
        runtime = _PhaseCompactionRuntime(
            skill,
            model="gpt-3.5-turbo",
            run_id="compaction-integration-test",
            phase_compaction_cfg=compact_cfg,
            # engine is constructed lazily (default path) to exercise that branch
        )

        result = await runtime.run(
            initial_input={"type": "test_input", "data": {}},
        )
    finally:
        litellm.acompletion = original_acompletion  # type: ignore[assignment]

    # Primary invariant: compaction event emitted at least once.
    compacted_events = _events_of_type(runtime.events, "phase_act_results_compacted")
    assert compacted_events, (
        "phase_act_results_compacted event must be emitted when act results "
        "exceed recent_act_turns_raw — wiring OSRuntime → PhaseExecutor is broken "
        "if this event is absent (= engine/cfg were not passed through)"
    )

    # Secondary invariant: run completed successfully (compaction is non-blocking).
    assert result is not None


# ---------------------------------------------------------------------------
# Tier 2: no-fire guard — short act loop does not emit compaction event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_osruntime_compaction_does_not_fire_for_short_act_loop() -> None:
    """Tier 2: phase_act_results_compacted is NOT emitted for short act loops.

    Invariant: the fire condition guard (len(control_ir_results) > recent_act_turns_raw)
    correctly suppresses compaction when the accumulated results count stays at or
    below the threshold.

    Setup: recent_act_turns_raw=5, n_act_turns=2 → 2 results < 5 → no compaction.
    """
    import litellm

    skill = _make_skill(max_act_turns=10)
    compact_cfg = PhaseActResultsCompactionConfig(
        recent_act_turns_raw=5,  # high threshold
        summarize_older_threshold_tokens=1,  # irrelevant, condition guards first
        use_chars4_estimate=True,
    )
    scripted_llm = _ScriptedLLMAcompletion(
        finish_artifact_type="result",
        n_act_turns=2,  # 2 act turns → 2 results, 2 < 5 = no compaction
    )

    original_acompletion = litellm.acompletion
    litellm.acompletion = scripted_llm  # type: ignore[assignment]
    try:
        runtime = _PhaseCompactionRuntime(
            skill,
            model="gpt-3.5-turbo",
            run_id="no-fire-guard-test",
            phase_compaction_cfg=compact_cfg,
        )
        result = await runtime.run(
            initial_input={"type": "test_input", "data": {}},
        )
    finally:
        litellm.acompletion = original_acompletion  # type: ignore[assignment]

    # Invariant: no compaction event emitted.
    compacted_events = _events_of_type(runtime.events, "phase_act_results_compacted")
    assert not compacted_events, (
        "phase_act_results_compacted must NOT fire when act results < recent_act_turns_raw"
    )

    # Run still completed.
    assert result is not None


# ---------------------------------------------------------------------------
# Tier 2: injection path wires engine to PhaseExecutor (behavior verification)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_osruntime_injected_engine_wired_to_phase_executor() -> None:
    """Tier 2: injected phase_compaction_engine reaches PhaseExecutor (event fires).

    Complements the accessor identity test by verifying BEHAVIOR: when a
    real engine is injected and act results exceed recent_act_turns_raw, the
    compaction fires — proving the injected engine was actually wired, not
    just stored as a dead reference.
    """
    import litellm

    skill = _make_skill(max_act_turns=10)
    engine = _make_engine()
    compact_cfg = _make_compact_cfg(recent_act_turns_raw=2)
    scripted_llm = _ScriptedLLMAcompletion(
        finish_artifact_type="result",
        n_act_turns=4,
    )

    original_acompletion = litellm.acompletion
    litellm.acompletion = scripted_llm  # type: ignore[assignment]
    try:
        runtime = _PhaseCompactionRuntime(
            skill,
            model="gpt-3.5-turbo",
            run_id="injection-wiring-test",
            phase_compaction_engine=engine,
            phase_compaction_cfg=compact_cfg,
        )
        await runtime.run(
            initial_input={"type": "test_input", "data": {}},
        )
    finally:
        litellm.acompletion = original_acompletion  # type: ignore[assignment]

    # Verify via behavior: compaction event must fire.
    compacted_events = _events_of_type(runtime.events, "phase_act_results_compacted")
    assert compacted_events, (
        "Injected phase_compaction_engine must reach PhaseExecutor — "
        "phase_act_results_compacted event absent means wiring failed"
    )
