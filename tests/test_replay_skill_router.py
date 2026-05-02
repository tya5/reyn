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
