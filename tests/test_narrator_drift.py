"""Tier 2b: LLMReplay drift-detection invariants for skill_narrator fixtures.

The LLMReplay subsystem guarantees that a cache miss raises ``MissingFixture``
loudly rather than silently falling back.  These tests pin that invariant
against the real ``skill_narrator`` fixture files so that:

  - any change to the narrator's ContextFrame (instructions, input_artifact,
    candidate_outputs, …) will be detected at replay time, not silently swallowed;
  - the ``LLMReplay._key()`` canonicalisation is stable under equivalent but
    differently-ordered inputs.

All four tests are **Tier 2b** (subsystem invariant): they exercise
``LLMReplay`` directly — not via ``call_llm`` — to prove the contract holds
at the boundary that guards every Tier 3a replay test.

No LLM calls are made; cost is 0.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.testing.replay import LLMReplay, MissingFixture, REPLAY_DATETIME
from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ExecutionState,
    PhaseConstraints,
)
from reyn.llm.llm import _system_prompt

# ── Fixture paths ─────────────────────────────────────────────────────────────

_FIXTURE_DIR = Path(__file__).parent / "fixtures/llm/skill_narrator"
_HAPPY_PATH_FIXTURE = _FIXTURE_DIR / "happy_path.jsonl"
_FAILED_FIXTURE = _FIXTURE_DIR / "failed_with_error.jsonl"

MODEL = "gemini-2.5-flash-lite"
SKILL_NAME = "skill_narrator"
SKILL_DESC = (
    "Convert a finished skill's structured `final_output` into a chat-friendly "
    "natural-language reply."
)


# ── Shared helpers ────────────────────────────────────────────────────────────


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


def _build_happy_frame() -> ContextFrame:
    """Construct the ContextFrame that matches the happy_path fixture."""
    return ContextFrame(
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


def _messages_for_frame(frame: ContextFrame) -> list[dict]:
    """Reproduce the messages list that ``call_llm`` would build for this frame.

    Mirrors the logic in ``reyn.llm.llm.call_llm`` with
    ``prompt_cache_enabled=False`` (no cache_control marker) so the messages
    are byte-identical to what the Tier 3a tests record.
    """
    system = _system_prompt(
        skill_name=SKILL_NAME,
        skill_description=SKILL_DESC,
        phase_role="narrator",
    )
    user_content = json.dumps(frame.model_dump(mode="json"), indent=2, ensure_ascii=False)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


# ── Test 1: wrong input raises MissingFixture ─────────────────────────────────


def test_narrator_wrong_input_raises_missing_fixture():
    """Tier 2b: LLMReplay raises MissingFixture when the ContextFrame differs from the fixture.

    The fixture key is a SHA-256 of (model + messages).  Changing any field
    that lands in the user-turn JSON (e.g. ``instructions``) produces a
    different key → MissingFixture.  This invariant is the mechanism that
    makes prompt drift visible at test time rather than silently wrong at
    production time.
    """
    frame = _build_happy_frame()
    # Mutate a field that is serialised into the user-turn message.
    frame = frame.model_copy(update={"instructions": "this instruction is NOT in the fixture"})
    messages = _messages_for_frame(frame)

    replay = LLMReplay(_HAPPY_PATH_FIXTURE, mode="replay")
    with pytest.raises(MissingFixture, match="No fixture entry"):
        replay._replay(
            key=LLMReplay._key(MODEL, messages),
            model=MODEL,
            messages=messages,
        )


# ── Test 2: correct input replays successfully ────────────────────────────────


def test_narrator_correct_input_replays_successfully():
    """Tier 2b: LLMReplay returns a ModelResponse when the input matches the happy_path fixture.

    Verifies the *positive* side of the drift-detection contract: a correctly
    constructed ContextFrame hits the fixture and produces a real
    ``litellm.ModelResponse`` (not a mock) — no LLM call is made.
    """
    import litellm

    frame = _build_happy_frame()
    messages = _messages_for_frame(frame)
    key = LLMReplay._key(MODEL, messages)

    replay = LLMReplay(_HAPPY_PATH_FIXTURE, mode="replay")
    # The fixture must exist; if it doesn't, the Tier 3a record tests need to run first.
    assert _HAPPY_PATH_FIXTURE.exists(), (
        f"Fixture missing: {_HAPPY_PATH_FIXTURE}. "
        "Run the Tier 3a tests with REYN_LLM_RECORD=1 first."
    )
    assert key in replay._records, (
        f"Key {key!r} not found in fixture — frame may have drifted from recording. "
        "Re-run Tier 3a test with REYN_LLM_RECORD=1 to update the fixture."
    )

    response = replay._replay(key=key, model=MODEL, messages=messages)
    assert isinstance(response, litellm.ModelResponse), (
        "Expected a real litellm.ModelResponse, not a mock or plain dict"
    )
    content = response.choices[0].message.content
    assert content is not None and len(content) > 0, (
        "Replayed response must have non-empty content"
    )


# ── Test 3: input canonicalisation — key is stable under messages-dict key order ─


def test_narrator_input_canonicalization():
    """Tier 2b: LLMReplay._key() is stable under different key-insertion order within message dicts.

    ``json.dumps(sort_keys=True)`` is applied to the messages list in
    ``LLMReplay._key()``.  This means the order of keys *inside each message
    dict* (e.g. ``{"role": ..., "content": ...}`` vs ``{"content": ...,
    "role": ...}``) does not affect the SHA-256 key.  The test pins this
    canonicalisation guarantee so a refactor of _key() that drops sort_keys is
    immediately caught.
    """
    frame = _build_happy_frame()
    messages_a = _messages_for_frame(frame)

    # Build a second messages list where each message dict has its keys in
    # reversed insertion order — semantically identical, structurally different.
    messages_b = [
        dict(reversed(list(msg.items()))) for msg in messages_a
    ]

    key_a = LLMReplay._key(MODEL, messages_a)
    key_b = LLMReplay._key(MODEL, messages_b)

    assert key_a == key_b, (
        "LLMReplay._key() must produce the same SHA-256 regardless of key "
        "insertion order inside the message dicts (sort_keys=True guarantee). "
        f"key_a={key_a!r}, key_b={key_b!r}"
    )


# ── Test 4: changing the artifact type field drifts the key ──────────────────


def test_narrator_input_artifact_type_drift_raises_missing_fixture():
    """Tier 2b: LLMReplay raises MissingFixture when input_artifact.type changes.

    ``input_artifact`` is serialised into the user-turn JSON.  Renaming the
    artifact type (a P7 concern — artifact types are skill-domain strings)
    must produce a cache miss, not a silent wrong-fixture hit.  This pins the
    invariant that even a shallow field change is detectable.
    """
    frame = _build_happy_frame()
    # Change the artifact type — a field present in the user-turn JSON.
    drifted_artifact = dict(frame.input_artifact)
    drifted_artifact["type"] = "narration_request_v2"  # type name changed
    frame = frame.model_copy(update={"input_artifact": drifted_artifact})
    messages = _messages_for_frame(frame)

    replay = LLMReplay(_HAPPY_PATH_FIXTURE, mode="replay")
    key = LLMReplay._key(MODEL, messages)
    with pytest.raises(MissingFixture, match="No fixture entry"):
        replay._replay(key=key, model=MODEL, messages=messages)
