"""Tier 2: OS invariant — SkillRegistry lifecycle hooks fire at the correct
moments inside OSRuntime.run().

Design choice (documented here per testing policy):
  We need to exercise OSRuntime.run() without an LLM.  The cleanest approach
  that satisfies the no-MagicMock policy is:

  1. ``_StubRuntime`` — a thin OSRuntime subclass that overrides
     ``_execute_phase`` to return a deterministic finish decision.  This is
     NOT a collaborator mock — it is subclassing the unit under test to
     replace one internal method that would require a real LLM.  The rest of
     run() executes real code.

  2. ``SpyRegistry`` — a SkillRegistry subclass that records calls to
     start / advance_phase / complete without requiring a real StateLog or
     filesystem (state_log=None path).  No unittest.mock is used.

Invariants tested (Tier 2 — P6: state mutations emit events / hooks):
  - start() fires once with the correct run_id and skill_name
  - advance_phase() fires for the entry phase immediately after _enter_phase
  - complete() fires even when run() raises (exception path)
  - With skill_registry=None, none of the above fire (backward compat)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from reyn.core.kernel.normalizer import NormalizationResult
from reyn.core.kernel.runtime import OSRuntime, WorkflowAbortedError
from reyn.schemas.models import (
    ControlDecision,
    ControlReason,
    LLMOutput,
    Phase,
    Skill,
    SkillGraph,
)
from reyn.skill.skill_registry import SkillRegistry

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_skill(
    *,
    name: str = "test_skill",
    phase_name: str = "only",
) -> Skill:
    """Build a minimal 1-phase skill that allows finishing from the entry phase."""
    phase = Phase(
        name=phase_name,
        instructions="do something",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name=name,
        entry_phase=phase_name,
        phases={phase_name: phase},
        graph=SkillGraph(
            transitions={},
            can_finish_phases=[phase_name],
        ),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _finish_decision() -> NormalizationResult:
    """Return a NormalizationResult for a finish decision."""
    ctrl = ControlDecision(
        type="finish",
        decision="finish",
        next_phase=None,
        confidence=1.0,
        reason=ControlReason(summary="done"),
    )
    return NormalizationResult(control=ctrl)


def _finish_output(schema_name: str = "result") -> LLMOutput:
    """Return a minimal LLMOutput that finishes cleanly."""
    ctrl = ControlDecision(
        type="finish",
        decision="finish",
        next_phase=None,
        confidence=1.0,
        reason=ControlReason(summary="done"),
    )
    return LLMOutput(
        control=ctrl,
        artifact={"type": schema_name, "data": {}},
        ops=[],
    )


# ── SpyRegistry ────────────────────────────────────────────────────────────────


@dataclass
class _Call:
    method: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class SpyRegistry(SkillRegistry):
    """SkillRegistry subclass that records lifecycle calls without touching disk.

    Constructed with no state_log and no agent_state_dir on disk, so file
    operations that SkillRegistry performs are safe: _save() would fail without
    the directory.  We override _save to be a no-op so we can focus on the
    call-recording invariants.
    """

    def __init__(self) -> None:
        # Provide a real-looking but unused tmp dir; no state_log.
        super().__init__(
            agent_name="spy_agent",
            agent_state_dir=Path("/tmp/spy_registry_unused"),
            state_log=None,
        )
        self.calls: list[_Call] = []

    def _save(self, snap: Any) -> None:  # type: ignore[override]
        """No-op: tests don't need disk persistence."""

    async def start(
        self, *, run_id: str, skill_name: str, skill_input: dict,
        parent_run_id: str | None = None,
    ) -> Any:
        self.calls.append(_Call("start", {
            "run_id": run_id, "skill_name": skill_name,
            "parent_run_id": parent_run_id,
        }))
        return await super().start(
            run_id=run_id, skill_name=skill_name, skill_input=skill_input,
            parent_run_id=parent_run_id,
        )

    async def advance_phase(
        self,
        *,
        run_id: str,
        next_phase: str,
        last_phase_artifact_path: str | None = None,
    ) -> None:
        self.calls.append(
            _Call("advance_phase", {"run_id": run_id, "next_phase": next_phase})
        )
        return await super().advance_phase(
            run_id=run_id,
            next_phase=next_phase,
            last_phase_artifact_path=last_phase_artifact_path,
        )

    async def complete(self, *, run_id: str) -> None:
        self.calls.append(_Call("complete", {"run_id": run_id}))
        return await super().complete(run_id=run_id)

    def called_methods(self) -> list[str]:
        return [c.method for c in self.calls]


# ── Stub runtime ───────────────────────────────────────────────────────────────


class _StubRuntime(OSRuntime):
    """OSRuntime with _execute_phase overridden to return a scripted finish.

    No LLM is invoked.  The rest of run() — event emission, workspace writes,
    SkillRegistry hooks — execute real code.
    """

    def __init__(self, skill: Skill, *, skill_registry: SkillRegistry | None = None) -> None:
        super().__init__(
            skill,
            model="stub/model",
            run_id="test-run-001",
            skill_registry=skill_registry,
        )

    async def _execute_phase(
        self,
        current_phase: str,
        artifact: dict,
        candidates: list,
        output_language: str,
        max_phase_retries: int,
        artifact_path: str | None = None,
        rollback_context: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        return _finish_decision(), _finish_output(), 0


class _RaisingStubRuntime(_StubRuntime):
    """Variant that raises WorkflowAbortedError on _execute_phase.

    Used to verify that complete() fires even when run() raises.
    """

    async def _execute_phase(
        self,
        current_phase: str,
        artifact: dict,
        candidates: list,
        output_language: str,
        max_phase_retries: int,
        artifact_path: str | None = None,
        rollback_context: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        raise WorkflowAbortedError("test-abort")


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_skill_registry_start_and_advance_on_success() -> None:
    """Tier 2: start() and advance_phase() fire when skill_registry is wired in.

    Invariant: on a successful run, the registry receives:
      1. start(run_id, skill_name)
      2. advance_phase(run_id, entry_phase)  ← entry phase advance
      3. complete(run_id)
    """
    skill = _make_skill()
    spy = SpyRegistry()
    rt = _StubRuntime(skill, skill_registry=spy)

    result = asyncio.run(rt.run({"type": "artifact", "data": {}}))

    assert result.ok, f"expected finished, got {result.status}"
    assert spy.called_methods() == ["start", "advance_phase", "complete"]

    start_call = spy.calls[0]
    assert start_call.kwargs["run_id"] == "test-run-001"
    assert start_call.kwargs["skill_name"] == "test_skill"

    advance_call = spy.calls[1]
    assert advance_call.kwargs["run_id"] == "test-run-001"
    assert advance_call.kwargs["next_phase"] == "only"


def test_skill_registry_complete_fires_on_exception() -> None:
    """Tier 2: complete() fires in the finally clause even when run() raises.

    Invariant: registry cleanup is guaranteed regardless of success/failure path.
    """
    skill = _make_skill()
    spy = SpyRegistry()
    rt = _RaisingStubRuntime(skill, skill_registry=spy)

    with pytest.raises(WorkflowAbortedError):
        asyncio.run(rt.run({"type": "artifact", "data": {}}))

    assert "complete" in spy.called_methods(), (
        "complete() must fire even when run() raises"
    )
    complete_call = next(c for c in spy.calls if c.method == "complete")
    assert complete_call.kwargs["run_id"] == "test-run-001"


def test_no_skill_registry_no_calls() -> None:
    """Tier 2: backward compat — with skill_registry=None, no registry calls fire.

    Verifies that the None-guard paths in run() do not introduce regressions
    for callers that don't wire a registry.
    """
    skill = _make_skill()
    # No skill_registry — should succeed silently
    rt = _StubRuntime(skill, skill_registry=None)

    result = asyncio.run(rt.run({"type": "artifact", "data": {}}))
    assert result.ok, f"expected finished, got {result.status}"
    # No assertion on spy — this test proves no AttributeError or call leaks.


def test_skill_registry_run_id_consistent() -> None:
    """Tier 2: run_id passed to start/advance_phase/complete is the same value.

    Invariant (P6): all lifecycle events for a single run share one stable run_id.
    """
    skill = _make_skill()
    spy = SpyRegistry()
    rt = _StubRuntime(skill, skill_registry=spy)
    asyncio.run(rt.run({"type": "artifact", "data": {}}))

    run_ids = {c.kwargs["run_id"] for c in spy.calls}
    assert run_ids == {"test-run-001"}, (
        f"All lifecycle calls must share one run_id; got {run_ids}"
    )
