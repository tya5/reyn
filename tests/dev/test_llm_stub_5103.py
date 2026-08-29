"""Tier 1: `LLMStub`'s OWN output contract (#5103 TESTS-READ, architect) —
`finish_reason`/`tool_calls` must be OUR explicit choice, not a silent
inheritance of ``litellm.ModelResponse``'s own default.

Before this test, ``finish_reason="stop"`` / ``tool_calls=None`` appeared
ONLY in ``LLMStub``'s docstring and the ``@llm_stub`` marker's registration
text — never in an assert. If litellm's own default ever changed, every
``@llm_stub`` test would silently start seeing something different: the
migrated tests assert on the LOOP side (did the turn complete, did the
counter decrement), not on the completion object itself, so nothing would
catch the drift. This test pins OUR stub's own construction directly,
independent of whatever litellm.ModelResponse happens to default to today.

#5382 addition: the ``raise_for="compaction"``/``cause=`` mode's own 5
witnesses (architect's table). Witness 3 (selectivity: the SAME run's
main router call still succeeds) is
``test_raise_for_compaction_leaves_the_main_router_call_untouched_end_to_end``
in ``tests/dev/test_5382_llm_stub_compaction_selectivity.py`` — a SEPARATE
file, deliberately (lead-coder BLOCKING, #5461: an earlier revision of
this docstring claimed this witness lived in
``test_5296_pr2_byte_reduction_same_turn_retry.py``; it did not — the
unit-level ``test_raise_for_compaction_does_not_touch_a_non_compaction_
call`` below uses a HAND-WRITTEN system-message string, which proves
nothing about what the REAL router actually places there. Once written
here, it needed a real Session/turn driving `force_compact_now`, and was
found to only pass co-located in its OWN file — some cross-test/module
interaction in THIS file's larger suite made the same code silently
swallow the raise; see that file's own module docstring for the
instrumented finding)."""
from __future__ import annotations

import pytest

from reyn.dev.testing.llm_stub import LLMStub
from reyn.dev.testing.replay import UnknownReplayCause
from reyn.prompt.compaction import COMPACTION_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_stub_response_is_a_terminal_no_tool_call_completion() -> None:
    """Tier 1: the stub's own explicitly-constructed response — not
    litellm's default — has finish_reason="stop" and no tool_calls, so the
    loop always sees "the model said nothing and asked for nothing"."""
    stub = LLMStub()

    response = await stub._handle("openai/some-model", [{"role": "user", "content": "hi"}])

    (choice,) = response.choices
    assert choice.finish_reason == "stop"
    assert choice.message.tool_calls is None
    assert choice.message.role == "assistant"


@pytest.mark.asyncio
async def test_stub_response_carries_the_requested_model_name() -> None:
    """Tier 1: the response's `model` field echoes whatever model was
    requested — the stub does not silently substitute a different one."""
    stub = LLMStub()

    response = await stub._handle("openai/a-specific-model", [])

    assert response.model == "openai/a-specific-model"


@pytest.mark.asyncio
async def test_stub_response_is_the_same_regardless_of_the_request() -> None:
    """Tier 1: two different requests (different messages/kwargs) get the
    SAME finish_reason/tool_calls shape — the stub genuinely ignores what
    was asked (the property `@llm_stub`'s Tier-3 ban, Rule 9, depends on)."""
    stub = LLMStub()

    r1 = await stub._handle("m", [{"role": "user", "content": "one"}])
    r2 = await stub._handle(
        "m", [{"role": "user", "content": "two"}], tools=[{"type": "function"}],
    )

    assert r1.choices[0].finish_reason == r2.choices[0].finish_reason == "stop"
    assert r1.choices[0].message.tool_calls is None
    assert r2.choices[0].message.tool_calls is None


@pytest.mark.asyncio
async def test_install_and_restore_round_trip_litellm_acompletion() -> None:
    """Tier 1: install() replaces litellm.acompletion; restore() puts the
    original back — the lifecycle @llm_stub's autouse fixture depends on."""
    import litellm

    original = litellm.acompletion
    stub = LLMStub()

    stub.install()
    try:
        assert litellm.acompletion is not original
        response = await litellm.acompletion(model="m", messages=[])
        assert response.choices[0].finish_reason == "stop"
    finally:
        stub.restore()

    assert litellm.acompletion is original


@pytest.mark.asyncio
async def test_raise_for_compaction_raises_the_named_cause() -> None:
    """Tier 1: #5382 witness 1/2 — a compaction-shaped call (system
    message == COMPACTION_SYSTEM_PROMPT) raises the real litellm
    exception for the given cause, when raise_for="compaction"."""
    import litellm

    stub = LLMStub(raise_for="compaction", cause="rate_limit")
    messages = [
        {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
        {"role": "user", "content": "anything"},
    ]

    with pytest.raises(litellm.RateLimitError):
        await stub._handle("m", messages)


@pytest.mark.asyncio
async def test_raise_for_compaction_does_not_touch_a_non_compaction_call() -> None:
    """Tier 1: #5382's central selectivity witness — a call whose system
    message is NOT COMPACTION_SYSTEM_PROMPT (the main router's own call)
    keeps the ordinary success response, even with raise_for="compaction"
    armed. Without this, "raise for compaction" would be indistinguishable
    from "raise for everything"."""
    stub = LLMStub(raise_for="compaction", cause="rate_limit")
    messages = [
        {"role": "system", "content": "you are the main chat router, not compaction"},
        {"role": "user", "content": "hello"},
    ]

    response = await stub._handle("m", messages)

    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.content == ""


@pytest.mark.asyncio
async def test_a_recognized_compaction_call_without_raise_for_gets_a_valid_summary() -> None:
    """Tier 1: a compaction-shaped call, with NO raise_for armed, gets a
    MINIMAL VALID ChatSummary JSON (topic_arc + the 4 required array
    fields) — not the ordinary "" response, which compact() itself would
    reject (#4883's own non-empty-topic_arc validation; confirmed
    empirically while designing this: content="" raises
    "compaction LLM returned empty response")."""
    import json

    stub = LLMStub()
    messages = [
        {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
        {"role": "user", "content": "anything"},
    ]

    response = await stub._handle("m", messages)

    parsed = json.loads(response.choices[0].message.content)
    assert parsed["topic_arc"], "topic_arc must be non-empty (#4883)"
    for field in ("decisions", "pending", "session_user_facts", "artifacts_referenced"):
        assert parsed[field] == []


@pytest.mark.asyncio
async def test_an_unrecognized_cause_raises_unknownreplaycause() -> None:
    """Tier 1: #5382's own closed-vocabulary guard, reused here — an
    unrecognised cause fails explicitly, not a silent fallback (same
    posture as LLMReplay's own UnknownReplayCause)."""
    stub = LLMStub(raise_for="compaction", cause="some_future_cause")
    messages = [
        {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
        {"role": "user", "content": "anything"},
    ]

    with pytest.raises(UnknownReplayCause):
        await stub._handle("m", messages)


def test_raise_for_and_cause_must_be_given_together() -> None:
    """Tier 1: strip witness — raise_for without cause (or vice versa) is
    a construction-time error, not a silently-ignored half-configuration."""
    with pytest.raises(ValueError):
        LLMStub(raise_for="compaction")
    with pytest.raises(ValueError):
        LLMStub(cause="rate_limit")


