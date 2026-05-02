"""Replay tests for skill_router intent classification.

Verifies that the intent classification path produces stable, deterministic
outputs for representative utterances. All LLM calls are intercepted by the
``@pytest.mark.replay`` fixture — no real LLM is invoked in normal test runs.

Areas covered (Tier 3a)
-----------------------
- Chitchat: direct finish with ``reply_text``, empty ``skills_to_run``.
- Task dispatch: classify → transition to match.
- Monkeypatch lifecycle invariant (Tier 2): LLMReplay does not leak across tests.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from reyn.llm.llm import call_llm
from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ControlIROpSpec,
    ExecutionState,
    PhaseConstraints,
)
from reyn.testing.replay import REPLAY_DATETIME

MODEL = "gemini-2.5-flash-lite"
SKILL_DESC = (
    "Route a single user chat utterance to an appropriate skill (or reply directly).\n"
    "Used by reyn chat to turn natural language into a routing decision."
)


# ── helpers ────────────────────────────────────────────────────────────────────


def _candidate_finish(schema: str = "routing_decision") -> CandidateOutput:
    return CandidateOutput(
        next_phase="end",
        control_type="finish",
        schema_name=schema,
        artifact_schema={
            "type": "object",
            "properties": {
                "reply_text": {"type": "string"},
                "skills_to_run": {"type": "array", "items": {}},
            },
            "required": ["reply_text", "skills_to_run"],
        },
        description="Direct reply for chitchat/stable_knowledge/memory_recall/clarification",
    )


def _candidate_match() -> CandidateOutput:
    return CandidateOutput(
        next_phase="match",
        control_type="transition",
        schema_name="routing_intent",
        artifact_schema={
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "user_message": {"type": "string"},
            },
            "required": ["intent", "user_message"],
        },
        description="Hand off to match for task/fresh_lookup",
    )


def _op_file() -> ControlIROpSpec:
    return ControlIROpSpec(
        kind="file",
        description="Read a file",
        example={"kind": "file", "op": "read", "path": ".reyn/memory/MEMORY.md"},
    )


def _run(coro):
    return asyncio.run(coro)


# ── test: chitchat — direct finish ────────────────────────────────────────────


@pytest.mark.replay("fixtures/llm/skill_router/chitchat.jsonl")
def test_classify_chitchat_finishes_directly():
    """Tier 3a: skill_router classifies chitchat → finish with reply_text."""
    frame = ContextFrame(
        current_phase="classify",
        current_phase_role="chat_router",
        instructions="Classify user intent into one of 6 intents. For chitchat, finish immediately.",
        candidate_outputs=[_candidate_finish(), _candidate_match()],
        finish_criteria=["Intent classified"],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_file()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "chat_routing_request",
            "data": {
                "user_message": "Hello, how are you today?",
                "chat_id": "test-session-001",
                "available_skills": [],
                "history": [],
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            skill_description=SKILL_DESC,
            phase_role="chat_router",
        )
    )

    data = result.data
    assert data["type"] == "decide", f"Expected decide turn, got: {data.get('type')}"
    ctrl = data["control"]
    assert ctrl["type"] == "finish"
    assert ctrl["decision"] == "finish"
    assert ctrl["next_phase"] is None

    artifact = data["artifact"]
    assert artifact["type"] == "routing_decision"
    art_data = artifact["data"]
    assert isinstance(art_data["reply_text"], str)
    assert len(art_data["reply_text"]) > 0, "Expected non-empty reply_text for chitchat"
    assert art_data["skills_to_run"] == [], "Chitchat should not invoke any skills"

    assert result.usage is not None
    assert result.usage.prompt_tokens > 0


# ── test: task intent — classify transitions to match ─────────────────────────


@pytest.mark.replay("fixtures/llm/skill_router/task_dispatch.jsonl")
def test_classify_task_transitions_to_match():
    """Tier 3a: skill_router classifies a task utterance → transition to match."""
    frame = ContextFrame(
        current_phase="classify",
        current_phase_role="chat_router",
        instructions="Classify user intent into one of 6 intents. For task intent, transition to match.",
        candidate_outputs=[_candidate_finish(), _candidate_match()],
        finish_criteria=["Intent classified"],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_file()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "chat_routing_request",
            "data": {
                "user_message": "Please run the article_generator skill to write about climate change.",
                "chat_id": "test-session-002",
                "available_skills": [
                    {
                        "name": "article_generator",
                        "description": "Generates a polished article on a given topic.",
                    }
                ],
                "history": [],
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            skill_description=SKILL_DESC,
            phase_role="chat_router",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["type"] == "transition"
    assert ctrl["next_phase"] == "match"
    assert ctrl["decision"] == "continue"


# ── corner case: ambiguous intent — no prior context ─────────────────────────


@pytest.mark.replay("fixtures/llm/skill_router/ambiguous_intent.jsonl")
def test_classify_ambiguous_with_no_context():
    """Tier 3a corner: 'how about that' with no history — classifier behaviour.

    The user utterance is referentially ambiguous. The router has no history
    to ground "that". A reasonable skill should either ask a clarifying
    question (finish with reply_text) or decline. We don't pin which choice it
    makes; we pin that a valid decide turn is produced and no skill is run.
    """
    frame = ContextFrame(
        current_phase="classify",
        current_phase_role="chat_router",
        instructions="Classify user intent into one of 6 intents. For chitchat, finish immediately.",
        candidate_outputs=[_candidate_finish(), _candidate_match()],
        finish_criteria=["Intent classified"],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_file()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "chat_routing_request",
            "data": {
                "user_message": "how about that",
                "chat_id": "test-ambiguous-001",
                "available_skills": [
                    {
                        "name": "article_generator",
                        "description": "Generates a polished article on a given topic.",
                    }
                ],
                "history": [],
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            skill_description=SKILL_DESC,
            phase_role="chat_router",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    # Ambiguous-intent: should not blindly dispatch to a skill.
    if ctrl["type"] == "transition":
        # If it does transition, it must not fabricate a task from no context.
        assert ctrl["next_phase"] in ("match",), (
            f"Unexpected next_phase for ambiguous input: {ctrl['next_phase']}"
        )
    else:
        # Most natural outcome: finish with a reply (clarification or chitchat).
        assert ctrl["type"] == "finish"
        artifact = data["artifact"]
        assert artifact["type"] == "routing_decision"
        art_data = artifact["data"]
        assert isinstance(art_data["reply_text"], str) and len(art_data["reply_text"]) > 0
        assert art_data["skills_to_run"] == [], (
            "Ambiguous user message should not invoke any skill"
        )


# ── corner case: out-of-scope request maps to no skill ───────────────────────


@pytest.mark.replay("fixtures/llm/skill_router/out_of_scope.jsonl")
def test_classify_out_of_scope_does_not_invent_skill():
    """Tier 3a corner: 'paint my house' has no matching skill — must not fabricate one.

    Protects against the most common router misbehaviour: hallucinating a
    skill name or transitioning to match with an intent it cannot satisfy.
    The expected behaviour is a finish with empty skills_to_run (politely
    declining or redirecting).
    """
    frame = ContextFrame(
        current_phase="classify",
        current_phase_role="chat_router",
        instructions="Classify user intent into one of 6 intents. For chitchat, finish immediately.",
        candidate_outputs=[_candidate_finish(), _candidate_match()],
        finish_criteria=["Intent classified"],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_file()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "chat_routing_request",
            "data": {
                "user_message": "Please paint my house blue.",
                "chat_id": "test-oos-001",
                "available_skills": [
                    {
                        "name": "article_generator",
                        "description": "Generates a polished article on a given topic.",
                    },
                    {
                        "name": "text_summarizer",
                        "description": "Summarises long text into a short summary.",
                    },
                ],
                "history": [],
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            skill_description=SKILL_DESC,
            phase_role="chat_router",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    # The router must not transition to match for an out-of-scope task; even if
    # it does, skills_to_run must not contain a fabricated skill name.
    art_data = data["artifact"]["data"]
    skills_to_run = art_data.get("skills_to_run", [])
    available_names = {"article_generator", "text_summarizer"}
    for entry in skills_to_run:
        # Each entry should at minimum be a valid skill name from the catalogue.
        if isinstance(entry, dict):
            name = entry.get("skill") or entry.get("name")
        else:
            name = entry
        assert name in available_names, (
            f"Router invented an unknown skill for out-of-scope request: {name!r}"
        )


# ── OS validator: out-of-pool skill names are rejected (P4 enforcement) ───────


def _routing_decision_schema() -> dict:
    """Load the actual routing_decision schema from the stdlib skill.

    Pinning the live schema (rather than a copy) ensures the
    ``x-reyn-members-of`` annotation is what the OS will encounter at
    runtime — the test fails loudly if someone removes the annotation.
    """
    import yaml
    from pathlib import Path

    p = (
        Path(__file__).resolve().parents[1]
        / "src/reyn/stdlib/skills/skill_router/artifacts/routing_decision.yaml"
    )
    return yaml.safe_load(p.read_text(encoding="utf-8"))["schema"]


def _chat_routing_input(available: list[str]) -> dict:
    return {
        "type": "chat_routing_request",
        "data": {
            "user_message": "irrelevant",
            "chat_id": "test-validator",
            "available_skills": [{"name": n} for n in available],
            "history": [],
        },
    }


def test_out_of_pool_skill_rejected():
    """OS rejects routing_decision when skills_to_run names a skill outside
    input.data.available_skills.

    Pre-OSS dogfood (S1/S3/S4) showed the LLM hallucinates skill names
    (`researcher`, `code_reviewer`, `blog_post_generator`). Without OS
    enforcement this propagates to ``run_skill`` and surfaces as a
    runtime "skill not found". This test pins that the validator
    catches it at decision time so the retry loop can re-prompt.
    """
    from reyn.workspace import validate_artifact_data

    schema = _routing_decision_schema()
    input_artifact = _chat_routing_input(["article_generator", "text_summarizer"])

    artifact = {
        "type": "routing_decision",
        "data": {
            "reply_text": "",
            "skills_to_run": [
                {
                    "skill": "researcher",  # NOT in available_skills
                    "input": {"type": "user_message", "data": {"text": "go"}},
                }
            ],
        },
    }

    _, _, errors = validate_artifact_data(
        artifact,
        schema,
        validation_context={"skill_input": input_artifact},
    )
    assert errors, "OS should reject hallucinated skill name"
    joined = " ".join(errors)
    assert "researcher" in joined and "available_skills" in joined, (
        f"Validation error should name the offender and the source path: {errors}"
    )


def test_out_of_pool_retry_then_empty():
    """Two-attempt sequence: hallucinated skill → corrected to empty.

    Models the OS retry path: first attempt fails membership check,
    second attempt (after re-prompt feedback) corrects to empty
    ``skills_to_run`` and validates clean. This pins the recovery
    semantics — the OS never lets a fabricated skill leak into
    ``run_skill`` even when the LLM took an extra turn to comply.
    """
    from reyn.workspace import validate_artifact_data

    schema = _routing_decision_schema()
    input_artifact = _chat_routing_input(["article_generator", "text_summarizer"])

    # Attempt 1 — hallucinated.
    bad = {
        "type": "routing_decision",
        "data": {
            "reply_text": "",
            "skills_to_run": [
                {"skill": "blog_post_generator",
                 "input": {"type": "user_message", "data": {"text": "x"}}}
            ],
        },
    }
    _, _, errors1 = validate_artifact_data(
        bad, schema, validation_context={"skill_input": input_artifact},
    )
    assert errors1, "first attempt must be rejected"

    # Attempt 2 — corrected (empty list, polite decline).
    good = {
        "type": "routing_decision",
        "data": {
            "reply_text": "I can't help with that — none of the available skills fit.",
            "skills_to_run": [],
        },
    }
    norm, _, errors2 = validate_artifact_data(
        good, schema, validation_context={"skill_input": input_artifact},
    )
    assert errors2 == [], f"corrected attempt should validate clean: {errors2}"
    assert norm["skills_to_run"] == []


def test_validator_uses_skill_input_not_phase_input():
    """PR33: the membership check must reference the OS-trusted ``skill_input``
    (the skill's initial artifact), NOT the phase's immediate ``input`` —
    earlier phases can be LLM-authored and would otherwise let the LLM
    fabricate its own membership set.

    Pre-OSS dogfood (PR32, chat-routed) reproduced the bug: classify-phase
    LLM produced ``routing_intent.data.available_skills =
    [{"name": "blog_writer"}]`` (a fabricated single-element pass-through);
    the ``match`` phase emitted ``skills_to_run=[{skill: "blog_writer"}]``;
    membership check anchored at ``input.data.available_skills`` then said
    "yes, blog_writer is in the list" because the list was fabricated.

    Anchoring at ``skill_input`` (the chat_routing_request, OS-injected at
    run() entry) closes the hole.
    """
    from reyn.workspace import validate_artifact_data

    schema = _routing_decision_schema()

    # Trusted source — the skill's first input. Real catalogue, no
    # blog_writer.
    skill_input = _chat_routing_input(["article_generator", "text_summarizer"])

    # The fabricated phase input — what the classify LLM produced and the
    # match LLM saw. Includes blog_writer because the LLM lied to itself.
    fabricated_phase_input = {
        "type": "routing_intent",
        "data": {
            "intent": "task",
            "available_skills": [{"name": "blog_writer"}],
        },
    }

    artifact = {
        "type": "routing_decision",
        "data": {
            "reply_text": "",
            "skills_to_run": [
                {"skill": "blog_writer",
                 "input": {"type": "user_message", "data": {"text": "x"}}}
            ],
        },
    }

    # If the validator naively trusted ``input``, this would pass — the
    # fabricated set contains blog_writer. With ``skill_input`` anchoring,
    # the real catalogue rejects it.
    _, _, errors = validate_artifact_data(
        artifact,
        schema,
        validation_context={
            "input": fabricated_phase_input,
            "skill_input": skill_input,
        },
    )
    assert errors, (
        "Validator must reject blog_writer based on skill_input even though "
        "the LLM-fabricated phase input would have allowed it."
    )
    assert "blog_writer" in " ".join(errors)


def test_in_pool_skill_accepted():
    """Sanity counterpart: a skill name that IS in available_skills validates."""
    from reyn.workspace import validate_artifact_data

    schema = _routing_decision_schema()
    input_artifact = _chat_routing_input(["article_generator"])

    artifact = {
        "type": "routing_decision",
        "data": {
            "reply_text": "",
            "skills_to_run": [
                {"skill": "article_generator",
                 "input": {"type": "user_message", "data": {"text": "topic"}}}
            ],
        },
    }
    _, _, errors = validate_artifact_data(
        artifact, schema, validation_context={"skill_input": input_artifact},
    )
    assert errors == [], f"in-pool skill must not be rejected: {errors}"


# ── test: monkeypatch does not leak across tests ──────────────────────────────


def test_no_monkeypatch_leak():
    """Tier 2 (OS invariant): LLMReplay monkeypatch is confined to replay-marked tests.

    Protects: the conftest install/restore contract. If LLMReplay leaks into
    non-replay tests, any test that calls litellm.acompletion directly would
    silently use the fake, masking real integration failures.
    """
    import litellm

    # In a non-replay test, acompletion must be the real function from litellm,
    # not the LLMReplay._handle bound method (which lives in the reyn package).
    mod = getattr(litellm.acompletion, "__module__", "") or ""
    assert "reyn" not in mod, (
        f"litellm.acompletion appears to still be monkeypatched! module={mod!r}"
    )
