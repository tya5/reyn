"""Unit tests for call_llm_tools — the tool_use variant of call_llm.

Uses monkeypatch on litellm.acompletion; no fixture infrastructure needed.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

import litellm


# ---------------------------------------------------------------------------
# Helpers to build fake litellm ModelResponse objects
# ---------------------------------------------------------------------------

def _make_response(
    content: str | None = None,
    tool_calls=None,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
):
    """Build a minimal fake litellm ModelResponse."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls

    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def _make_tool_call(id_: str, name: str, arguments: str):
    tc = MagicMock()
    tc.id = id_
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

MINIMAL_MESSAGES = [{"role": "user", "content": "hi"}]
MINIMAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "run a skill",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


@pytest.mark.asyncio
async def test_call_llm_tools_returns_text_when_no_tool_calls(monkeypatch):
    """When the LLM returns plain text with no tool_calls, result reflects that."""
    fake = _make_response(content="hello", tool_calls=None)
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=fake))

    from reyn.llm.llm import call_llm_tools

    result = await call_llm_tools(
        model="openai/gpt-4o",
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
    )

    assert result.content == "hello"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_call_llm_tools_returns_normalized_tool_calls(monkeypatch):
    """tool_calls are normalized to plain dicts (no litellm internals)."""
    tc = _make_tool_call(
        id_="call_abc123",
        name="run_skill",
        arguments='{"skill": "hello"}',
    )
    fake = _make_response(content=None, tool_calls=[tc], finish_reason="tool_calls")
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=fake))

    from reyn.llm.llm import call_llm_tools

    result = await call_llm_tools(
        model="openai/gpt-4o",
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
    )

    assert len(result.tool_calls) == 1
    tc_out = result.tool_calls[0]
    # Must be a plain dict, not a MagicMock
    assert isinstance(tc_out, dict)
    assert tc_out["id"] == "call_abc123"
    assert tc_out["type"] == "function"
    assert isinstance(tc_out["function"], dict)
    assert tc_out["function"]["name"] == "run_skill"
    assert tc_out["function"]["arguments"] == '{"skill": "hello"}'


@pytest.mark.asyncio
async def test_stream_false_is_forced(monkeypatch):
    """stream=False must be passed to litellm regardless of caller input."""
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _make_response(content="ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from reyn.llm.llm import call_llm_tools

    await call_llm_tools(
        model="openai/gpt-4o",
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
    )

    assert captured.get("stream") is False


@pytest.mark.asyncio
async def test_response_format_not_passed(monkeypatch):
    """response_format must NOT be passed (incompatible with tools= on most providers)."""
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _make_response(content="ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from reyn.llm.llm import call_llm_tools

    await call_llm_tools(
        model="openai/gpt-4o",
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
    )

    assert "response_format" not in captured


@pytest.mark.asyncio
async def test_tools_and_tool_choice_passed_through(monkeypatch):
    """tools and tool_choice arrive at litellm verbatim."""
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _make_response(content="ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from reyn.llm.llm import call_llm_tools

    custom_tools = [{"type": "function", "function": {"name": "custom"}}]
    await call_llm_tools(
        model="openai/gpt-4o",
        messages=MINIMAL_MESSAGES,
        tools=custom_tools,
        tool_choice="required",
    )

    assert captured["tools"] == custom_tools
    assert captured["tool_choice"] == "required"


# ---------------------------------------------------------------------------
# Budget tracking tests (PR37 E)
# ---------------------------------------------------------------------------

def _make_budget_tracker(*, per_agent_tokens_hard: int | None = None):
    """Build a real BudgetTracker with optional per-agent token cap."""
    from reyn.budget.budget import BudgetTracker, CostConfig, CostLimitConfig
    cfg = CostConfig()
    if per_agent_tokens_hard is not None:
        cfg.per_agent_tokens = CostLimitConfig(hard_limit=per_agent_tokens_hard)
    return BudgetTracker(cfg)


@pytest.mark.asyncio
async def test_call_llm_tools_records_tokens_to_budget(monkeypatch):
    """Budget tracker accumulates prompt+completion tokens after a call."""
    fake = _make_response(content="hello", tool_calls=None,
                          prompt_tokens=42, completion_tokens=17)
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=fake))

    from reyn.llm.llm import call_llm_tools

    tracker = _make_budget_tracker()
    await call_llm_tools(
        model="openai/gpt-4o",
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
        budget=tracker,
        budget_agent="test-agent",
    )

    snap = tracker.snapshot()
    assert snap["agent_tokens"].get("test-agent", 0) == 42 + 17
    # daily / monthly counters should also reflect the call
    assert snap["daily_tokens"] == 42 + 17
    assert snap["monthly_tokens"] == 42 + 17


@pytest.mark.asyncio
async def test_call_llm_tools_pre_check_blocks_when_over_quota(monkeypatch):
    """When per-agent token cap is exhausted, call raises BudgetExceeded before
    calling litellm."""
    called = []

    async def fake_acompletion(**kwargs):
        called.append(True)
        return _make_response(content="should not reach here")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from reyn.llm.llm import call_llm_tools
    from reyn.budget.budget import BudgetExceeded

    # Set hard limit of 10 tokens, then fill it up with a real record
    tracker = _make_budget_tracker(per_agent_tokens_hard=10)
    from reyn.llm.pricing import TokenUsage
    tracker.record_llm(
        model="openai/gpt-4o",
        agent="test-agent",
        usage=TokenUsage(prompt_tokens=8, completion_tokens=5),  # total=13 > 10
    )

    with pytest.raises(BudgetExceeded):
        await call_llm_tools(
            model="openai/gpt-4o",
            messages=MINIMAL_MESSAGES,
            tools=MINIMAL_TOOLS,
            budget=tracker,
            budget_agent="test-agent",
        )

    # litellm must NOT have been called
    assert called == []


@pytest.mark.asyncio
async def test_call_llm_tools_no_budget_kwarg_skips_tracking(monkeypatch):
    """Backward compat: budget=None (default) → no tracking, no checks."""
    fake = _make_response(content="ok", prompt_tokens=99, completion_tokens=50)
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=fake))

    from reyn.llm.llm import call_llm_tools

    # No budget kwarg — should work exactly as before with no side effects
    result = await call_llm_tools(
        model="openai/gpt-4o",
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
    )
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_call_llm_records_tokens_to_budget(monkeypatch):
    """call_llm also records tokens when budget kwarg is provided."""
    import json

    payload = json.dumps({"type": "decide", "control": {"type": "finish", "decision": "finish",
                         "next_phase": None, "confidence": 1.0, "reason": {"summary": "done"}},
                         "artifact": {"type": "x", "data": {}}, "ops": []})
    fake = _make_response(content=payload, prompt_tokens=55, completion_tokens=22)
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=fake))

    from reyn.llm.llm import call_llm
    from reyn.schemas.models import ContextFrame

    tracker = _make_budget_tracker()
    frame = ContextFrame(
        current_phase="test",
        instructions="test instructions",
        input_artifact={},
        candidate_outputs=[],
        output_language="en",
    )
    await call_llm(
        "openai/gpt-4o",
        frame,
        budget=tracker,
        budget_agent="skill-agent",
    )

    snap = tracker.snapshot()
    assert snap["agent_tokens"].get("skill-agent", 0) == 55 + 22


@pytest.mark.asyncio
async def test_call_llm_pre_check_blocks_when_over_quota(monkeypatch):
    """call_llm raises BudgetExceeded before calling litellm when cap exceeded."""
    called = []

    async def fake_acompletion(**kwargs):
        called.append(True)
        return _make_response(content="{}")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from reyn.llm.llm import call_llm
    from reyn.budget.budget import BudgetExceeded
    from reyn.llm.pricing import TokenUsage
    from reyn.schemas.models import ContextFrame

    tracker = _make_budget_tracker(per_agent_tokens_hard=5)
    tracker.record_llm(
        model="openai/gpt-4o",
        agent="skill-agent",
        usage=TokenUsage(prompt_tokens=4, completion_tokens=3),  # total=7 > 5
    )

    frame = ContextFrame(
        current_phase="test",
        instructions="test instructions",
        input_artifact={},
        candidate_outputs=[],
        output_language="en",
    )
    with pytest.raises(BudgetExceeded):
        await call_llm(
            "openai/gpt-4o",
            frame,
            budget=tracker,
            budget_agent="skill-agent",
        )

    assert called == []
