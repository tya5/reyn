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
