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
"""
from __future__ import annotations

import pytest

from reyn.dev.testing.llm_stub import LLMStub


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
