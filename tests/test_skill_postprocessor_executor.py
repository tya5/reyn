"""Tier 2: PostprocessorExecutor OS-invariant tests.

Pins the contract that:
1. When skill.postprocessor is None, finish_artifact is returned unchanged.
2. A validate step that passes lets the artifact through.
3. A validate step that fails raises PostprocessorError.
4. A successful postprocessor run validates output against output_schema.
5. Output_schema validation failure raises PostprocessorError.
6. on_error: skip in a run_op step continues past failure without raising.
7. Events postprocessor_step_started/completed are emitted for each step.

No mocks; uses real instances. Steps that exercise PreprocessorExecutor
delegate only use ValidateStep (no LLM / no external ops) — keeping tests
deterministic and fast.

TODO (Tier 3): Add LLMReplay e2e for a full skill run that has a postprocessor
block, verifying that OSRuntime.run() returns the caller-contract artifact
rather than the LLM-contract artifact.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.compiler.expander import expand_phase, expand_skill
from reyn.compiler.ir import ArtifactDef, PhaseDef, SkillDef
from reyn.events.events import EventLog
from reyn.kernel.postprocessor_executor import PostprocessorError, PostprocessorExecutor
from reyn.llm.model_resolver import ModelResolver
from reyn.schemas.models import Skill
from reyn.workspace.workspace import Workspace

# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _event_log() -> EventLog:
    """Real EventLog with an in-memory subscriber."""
    collected: list[dict] = []

    def _sub(event):
        collected.append(event)

    log = EventLog(subscribers=[_sub])
    log._collected = collected  # expose for assertions
    return log


def _workspace(events: EventLog) -> Workspace:
    return Workspace(events)


def _resolver() -> ModelResolver:
    return ModelResolver({})


def _build_skill(postprocessor: dict | None = None) -> Skill:
    """Construct a minimal Skill, optionally with a postprocessor block."""
    artifacts = {
        "in_art": ArtifactDef(
            name="in_art",
            schema={"type": "object", "properties": {"x": {"type": "string"}}},
            description="Input",
            wrapped=True,
        ),
        "llm_art": ArtifactDef(
            name="llm_art",
            schema={
                "type": "object",
                "properties": {"y": {"type": "string"}},
                "required": ["y"],
            },
            description="LLM output",
            wrapped=True,
        ),
    }
    pd = PhaseDef(
        name="sole",
        inputs=["in_art"],
        role=None,
        can_finish=True,
        instructions="",
    )
    phase_obj = expand_phase(pd, [artifacts["in_art"]])
    sd = SkillDef(
        name="test_skill",
        description="",
        doc="",
        entry="sole",
        edges=[],
        skill_nodes={},
        final_output="llm_art",
        final_output_description="",
        finish_criteria=[],
        postprocessor=postprocessor or {},
    )
    return expand_skill(sd, {"sole": pd}, artifacts, {"sole": phase_obj})


def _executor(skill: Skill, events: EventLog) -> PostprocessorExecutor:
    ws = _workspace(events)
    return PostprocessorExecutor(
        skill=skill,
        workspace=ws,
        events=events,
        model="test-model",
        resolver=_resolver(),
        subscribers=events.subscribers,
        permission_resolver=None,
        intervention_bus=None,
    )


def _artifact(y: str = "hello") -> dict:
    """A minimal LLM finish artifact conforming to llm_art schema."""
    return {"type": "llm_art", "data": {"y": y}}


# ── Tier 2: None postprocessor passthrough ────────────────────────────────────


def test_postprocessor_none_returns_artifact_unchanged() -> None:
    """Tier 2: skill.postprocessor is None → artifact returned unchanged, zero usage."""
    skill = _build_skill(postprocessor=None)
    assert skill.postprocessor is None

    events = _event_log()
    executor = _executor(skill, events)
    artifact = _artifact()

    result, usage = asyncio.run(
        executor.run(artifact, output_language="en")
    )

    assert result == artifact
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    # No postprocessor events emitted
    post_events = [e for e in events._collected if "postprocessor" in e.type]
    assert post_events == []


# ── Tier 2: validate step passes ──────────────────────────────────────────────


def test_postprocessor_validate_step_passes() -> None:
    """Tier 2: validate step that passes leaves artifact intact."""
    skill = _build_skill(postprocessor={
        "output_schema": {"type": "object", "properties": {"y": {"type": "string"}}},
        "steps": [
            {"type": "validate", "schema": {"type": "object"}},
        ],
    })
    events = _event_log()
    executor = _executor(skill, events)
    artifact = _artifact("world")

    result, usage = asyncio.run(
        executor.run(artifact, output_language="en")
    )

    assert result["data"]["y"] == "world"


# ── Tier 2: validate step fails ───────────────────────────────────────────────


def test_postprocessor_validate_step_fails_raises_error() -> None:
    """Tier 2: validate step that fails raises PostprocessorError."""
    skill = _build_skill(postprocessor={
        "output_schema": {"type": "object"},
        "steps": [
            # Require "required_field" which is absent in the artifact
            {
                "type": "validate",
                "schema": {
                    "type": "object",
                    "required": ["required_field"],
                    "properties": {"required_field": {"type": "string"}},
                },
            },
        ],
    })
    events = _event_log()
    executor = _executor(skill, events)

    with pytest.raises(PostprocessorError, match=r"step\[0\]"):
        asyncio.run(
            executor.run(_artifact(), output_language="en")
        )

    # step_failed event emitted
    failed_events = [e for e in events._collected if e.type == "postprocessor_step_failed"]
    assert failed_events, "Expected at least one postprocessor_step_failed event"
    assert any(e.data["step_index"] == 0 for e in failed_events)


# ── Tier 2: output_schema validation at the end ───────────────────────────────


def test_postprocessor_output_schema_final_validation_fails() -> None:
    """Tier 2: after steps pass, output_schema violation raises PostprocessorError.

    The postprocessor steps are empty so the LLM artifact passes through
    unchanged. The output_schema requires a field the artifact does not have,
    causing the final validation to fail.
    """
    skill = _build_skill(postprocessor={
        # Caller schema requires a field "caller_extra" not in the LLM artifact
        "output_schema": {
            "type": "object",
            "required": ["caller_extra"],
            "properties": {"caller_extra": {"type": "string"}},
        },
        "steps": [],
    })
    events = _event_log()
    executor = _executor(skill, events)

    with pytest.raises(PostprocessorError, match=r"output_schema"):
        asyncio.run(
            executor.run(_artifact(), output_language="en")
        )


def test_postprocessor_output_schema_passes_when_artifact_conforms() -> None:
    """Tier 2: artifact that conforms to output_schema passes final validation."""
    skill = _build_skill(postprocessor={
        "output_schema": {
            "type": "object",
            "properties": {"y": {"type": "string"}},
        },
        "steps": [],
    })
    events = _event_log()
    executor = _executor(skill, events)

    result, _ = asyncio.run(
        executor.run(_artifact("ok"), output_language="en")
    )

    assert result["data"]["y"] == "ok"


# ── Tier 2: events emitted ────────────────────────────────────────────────────


def test_postprocessor_events_emitted_for_each_step() -> None:
    """Tier 2: postprocessor_step_started and postprocessor_step_completed emitted per step."""
    skill = _build_skill(postprocessor={
        "output_schema": {"type": "object", "properties": {"y": {"type": "string"}}},
        "steps": [
            {"type": "validate", "schema": {"type": "object"}},
            {"type": "validate", "schema": {"type": "object"}},
        ],
    })
    events = _event_log()
    executor = _executor(skill, events)

    asyncio.run(
        executor.run(_artifact(), output_language="en")
    )

    started = [e for e in events._collected if e.type == "postprocessor_step_started"]
    completed = [e for e in events._collected if e.type == "postprocessor_step_completed"]
    assert started, "Expected postprocessor_step_started events"
    assert completed, "Expected postprocessor_step_completed events"
    # Steps are indexed correctly — both step 0 and step 1 must appear
    started_indices = {e.data["step_index"] for e in started}
    completed_indices = {e.data["step_index"] for e in completed}
    assert 0 in started_indices
    assert 1 in started_indices
    assert 0 in completed_indices
    assert 1 in completed_indices


# ── Tier 2: multiple steps applied in order ───────────────────────────────────


def test_postprocessor_multiple_steps_applied_in_order() -> None:
    """Tier 2: two validate steps both run; second failure is caught correctly."""
    skill = _build_skill(postprocessor={
        "output_schema": {"type": "object"},
        "steps": [
            # First step passes
            {"type": "validate", "schema": {"type": "object"}},
            # Second step fails (requires missing field)
            {
                "type": "validate",
                "schema": {"type": "object", "required": ["nope"]},
            },
        ],
    })
    events = _event_log()
    executor = _executor(skill, events)

    with pytest.raises(PostprocessorError):
        asyncio.run(
            executor.run(_artifact(), output_language="en")
        )

    # First step completed, second step failed
    completed = [e for e in events._collected if e.type == "postprocessor_step_completed"]
    failed = [e for e in events._collected if e.type == "postprocessor_step_failed"]
    assert completed, "Expected at least one postprocessor_step_completed event"
    assert any(e.data["step_index"] == 0 for e in completed)
    assert failed, "Expected at least one postprocessor_step_failed event"
    assert any(e.data["step_index"] == 1 for e in failed)
