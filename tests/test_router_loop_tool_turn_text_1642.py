"""Tier 3a: #1642 — assistant TEXT content that accompanies tool_calls is surfaced
to the conversation, not dropped.

When an LLM response carries BOTH ``tool_calls`` and ``content`` (e.g. "Let me read
that file first." + a file__read call), the explanatory text must appear in the
conversation. Pre-#1642 the only site that emitted ``result.content`` to the outbox was
the no-tool_calls terminal text-reply path, so on a tool-turn the text was persisted to
history but never displayed. The fix emits an ``agent`` outbox bubble at the start of the
Execute arm (before tool execution), with meta ``source="router_tool_turn_text"``.

Driven via the ``call_llm_tools`` scripted-injection seam + ``FakeRouterHost`` (real Fake
that records ``put_outbox`` on its public ``.outbox`` list) — no mocks; asserts on the
public outbox surface (testing.ja.md).
"""
from __future__ import annotations

import json

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from tests._support.router_loop import (
    FakeRouterHost,
    make_loop,
    text_result,
)
from tests._support.router_loop import (
    ScriptedLLM as _ScriptedLLM,
)

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_and_tool(content: str, *, skill: str) -> LLMToolCallResult:
    """An LLM response carrying BOTH text content AND a tool_call (the #1642 case)."""
    return LLMToolCallResult(
        content=content,
        tool_calls=[
            {
                "id": "t1",
                "type": "function",
                "function": {
                    "name": "invoke_skill",
                    "arguments": json.dumps(
                        {"name": skill, "input": {"type": "Foo", "data": {}}}
                    ),
                },
            }
        ],
        finish_reason="tool_calls",
        usage=_USAGE,
    )


@pytest.mark.asyncio
async def test_tool_turn_text_content_surfaced_to_conversation(monkeypatch):
    """Tier 3a: a turn with content + tool_calls emits the content as an ``agent``
    bubble (the fix), in addition to the terminal text on the final no-tool turn."""
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)
    script = [
        _text_and_tool("Let me run your skill first.", skill="my_skill"),  # Execute turn
        text_result("All done."),                                          # terminal turn
    ]
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _ScriptedLLM(script))
    await loop.run("run my skill", [])

    agent = [m for m in host.outbox if m["kind"] == "agent"]
    texts = [m["text"] for m in agent]
    # The tool-turn's accompanying text is surfaced (pre-#1642 it was dropped) ...
    assert "Let me run your skill first." in texts
    # ... and the terminal text-reply still emits (existing behavior, no regression).
    assert "All done." in texts
    # The tool-turn text is emitted exactly once (no double-emit), via the fix's marker.
    fix_texts = [
        m["text"] for m in agent if m["meta"].get("source") == "router_tool_turn_text"
    ]
    assert fix_texts == ["Let me run your skill first."]


@pytest.mark.asyncio
async def test_no_tool_calls_turn_emits_content_once(monkeypatch):
    """Tier 3a: a no-tool_calls turn emits its content once via the terminal path only —
    NOT also via the tool-turn fix (which is Execute-arm-only). Guards against double-emit."""
    host = FakeRouterHost()
    loop = make_loop(host)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _ScriptedLLM([text_result("just text")])
    )
    await loop.run("hi", [])

    agent = [m for m in host.outbox if m["kind"] == "agent"]
    assert [m["text"] for m in agent] == ["just text"]  # exactly one, no duplicate
    assert not any(m["meta"].get("source") == "router_tool_turn_text" for m in agent)


@pytest.mark.asyncio
async def test_tool_turn_empty_content_skipped(monkeypatch):
    """Tier 3a: a tool-turn with empty/whitespace content emits NO agent bubble on the
    tool-turn (no empty-bubble noise) — only the terminal text appears."""
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)
    script = [
        _text_and_tool("   ", skill="my_skill"),  # whitespace-only content + tool_call
        text_result("done"),
    ]
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _ScriptedLLM(script))
    await loop.run("go", [])

    agent = [m for m in host.outbox if m["kind"] == "agent"]
    assert not any(m["meta"].get("source") == "router_tool_turn_text" for m in agent)
    assert [m["text"] for m in agent] == ["done"]  # only the terminal text
