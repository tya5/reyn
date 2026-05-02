"""Replay tests for skill_narrator.

skill_narrator is invoked internally by ChatSession after every skill spawn
completes. It converts the structured `final_output` of the finished skill
into a chat-friendly natural-language reply (`reply_text`).

Two scenarios:
1. Happy path: status="finished", structured result → narrator emits a
   non-empty, JSON-free reply_text.
2. Failure path: status != "finished", error result → narrator either
   surfaces the failure mode in plain language, or — if it cannot — the
   ChatSession fallback raw-dump path takes over (lines 2046-2062 in
   src/reyn/chat/session.py).

Tier 3a: two cases.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.llm.llm import call_llm
from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ExecutionState,
    PhaseConstraints,
)
from reyn.testing.replay import REPLAY_DATETIME

MODEL = "gemini-2.5-flash-lite"
SKILL_NAME = "skill_narrator"
SKILL_DESC = (
    "Convert a finished skill's structured `final_output` into a chat-friendly "
    "natural-language reply."
)


def _run(coro):
    return asyncio.run(coro)


def _candidate_narration_result() -> CandidateOutput:
    return CandidateOutput(
        next_phase="end",
        control_type="finish",
        schema_name="narration_result",
        artifact_schema={
            "type": "object",
            "properties": {
                "reply_text": {"type": "string"},
            },
            "required": ["reply_text"],
        },
        description="Friendly chat reply describing what the finished skill did",
    )


# ── happy path ───────────────────────────────────────────────────────────────


@pytest.mark.replay("fixtures/llm/skill_narrator/happy_path.jsonl")
def test_narrate_finished_skill_produces_friendly_text():
    """Tier 3a: skill_done with a normal result → narrator produces a non-empty, JSON-free sentence."""
    frame = ContextFrame(
        current_phase="narrate",
        current_phase_role="narrator",
        instructions=(
            "Convert the finished skill run described in `data` into a short, "
            "chat-friendly natural-language reply for the user. Never dump JSON. "
            "Keep it to one or two short sentences."
        ),
        candidate_outputs=[_candidate_narration_result()],
        finish_criteria=["reply_text is non-empty"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "narration_request",
            "data": {
                "skill": "article_generator",
                "status": "finished",
                "result": {
                    "title": "The State of Quantum Computing in 2026",
                    "word_count": 842,
                    "output_path": "out/articles/quantum_2026.md",
                    "topic": "quantum computing",
                },
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=0,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role="narrator",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["type"] == "finish"
    assert ctrl["decision"] == "finish"
    assert ctrl["next_phase"] is None

    artifact = data["artifact"]
    assert artifact["type"] == "narration_result"
    reply_text = artifact["data"].get("reply_text", "")
    assert isinstance(reply_text, str)
    assert len(reply_text.strip()) > 0, "Empty reply_text triggers the raw-dump fallback path"
    # Per skill.md: never dump JSON. A reply that is itself a JSON object dump
    # is the failure mode this skill exists to prevent.
    assert not (reply_text.strip().startswith("{") and reply_text.strip().endswith("}")), (
        f"Narrator must not dump raw JSON: {reply_text!r}"
    )


# ── failure path: skill_done with error → narrator may fall back ─────────────


@pytest.mark.replay("fixtures/llm/skill_narrator/failed_with_error.jsonl")
def test_narrate_failed_skill_describes_failure_or_falls_back():
    """Tier 3a: skill_done with status='loop_limit_exceeded' and error result.

    Per session.py lines 2046-2062, ChatSession's raw-dump fallback fires
    when narration returns empty text or raises. The narrator's
    contractual responsibility (skill.md): describe the failure mode in
    plain language. We pin: the call returns a valid decide turn — if the
    text is empty the caller's fallback engages, which is itself a tested
    code path. We assert non-empty here to catch the case where narrator
    mis-formats, leaving users to see ugly raw JSON.
    """
    frame = ContextFrame(
        current_phase="narrate",
        current_phase_role="narrator",
        instructions=(
            "Convert the finished skill run described in `data` into a short, "
            "chat-friendly natural-language reply for the user. For non-finished "
            "statuses, briefly explain what didn't complete and suggest the most "
            "likely fix."
        ),
        candidate_outputs=[_candidate_narration_result()],
        finish_criteria=["reply_text is non-empty"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "narration_request",
            "data": {
                "skill": "article_generator",
                "status": "loop_limit_exceeded",
                "result": {
                    "error": "max_phase_visits=5 reached without producing a final article",
                    "last_phase": "review_article",
                    "iterations": 5,
                },
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=0,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role="narrator",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["type"] == "finish"

    artifact = data["artifact"]
    assert artifact["type"] == "narration_result"
    reply_text = artifact["data"].get("reply_text", "")
    # An empty reply triggers the raw-dump fallback in session.py — that is a
    # legitimate path but indicates a narrator miss. We pin non-empty here so
    # a regression in narration of failure cases is caught.
    assert isinstance(reply_text, str) and len(reply_text.strip()) > 0, (
        "Narrator returned empty reply_text for a failed-skill status — caller's "
        "raw-dump fallback would engage in production"
    )
    # Failure narration should mention the failure mode in some recognisable form.
    lowered = reply_text.lower()
    assert any(
        kw in lowered
        for kw in (
            "limit", "loop", "fail", "didn't", "could not", "couldn't",
            "unable", "exceed", "incomplete", "not complete", "issue",
            "encountered", "too many", "many times", "without completing",
            "problem", "ran out", "try again", "retry",
        )
    ), (
        f"Failed-skill narration should describe the failure: {reply_text!r}"
    )
