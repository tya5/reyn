"""Tier 2: OS invariant — force-close call mechanism (#1092 PR-B).

The cumulative-axis force-close call turns the CURRENT turn into a clean finish
instead of letting it overflow: it swaps the main system prompt for the
axis-independent wrap-up SP (``services/turn_budget``) and advertises NO tools,
so the model consolidates rather than continues (§3). These pin:

- ``messages[0]`` becomes the wrap-up SP; the working (non-system) history is
  preserved verbatim;
- ``tools=[]`` (continuation suppression) with ``tool_choice="auto"`` (NOT the
  Gemini-unsafe ``"none"``);
- the call returns the model's finish result;
- only the wrap-up SP is the system context (extra system turns dropped).

No mocks: a real ``RouterLoop`` + ``FakeRouterHost`` + a real capturing
callable (signature drift raises, unlike AsyncMock).
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.services.turn_budget import wrap_up_system_prompt
from tests.test_router_loop import FakeRouterHost


class _CapturingLLM:
    """Real callable replacing call_llm_tools — records each call's kwargs and
    returns a canned finish. Policy-compliant (real ``__call__``, not a Mock)."""

    def __init__(self, result: LLMToolCallResult) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls.append(kwargs)
        return self._result


def _finish(text: str = "consolidated handoff") -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text, tool_calls=[], finish_reason="stop",
        usage=TokenUsage(prompt_tokens=50, completion_tokens=10),
    )


def _loop(llm: _CapturingLLM) -> RouterLoop:
    return RouterLoop(
        host=FakeRouterHost(), chain_id="chain-fc", max_iterations=5,
        llm_caller=llm,
    )


@pytest.mark.asyncio
async def test_force_close_swaps_system_prompt_to_wrap_up_sp() -> None:
    """Tier 2: the force-close call replaces the system turn with the wrap-up SP
    and preserves the working (non-system) history verbatim."""
    llm = _CapturingLLM(_finish())
    loop = _loop(llm)
    messages = [
        {"role": "system", "content": "ORIGINAL MAIN SP"},
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "working..."},
        {"role": "tool", "content": "big tool result", "tool_call_id": "x"},
    ]
    await loop._force_close_call(messages, resolved_model="gpt-4o-mini")

    sent = llm.calls[-1]["messages"]
    assert sent[0] == {"role": "system", "content": wrap_up_system_prompt()}
    assert "ORIGINAL MAIN SP" not in [m.get("content") for m in sent]
    assert sent[1:] == messages[1:]  # working history preserved in order


@pytest.mark.asyncio
async def test_force_close_suppresses_continuation_with_empty_tools() -> None:
    """Tier 2: continuation suppression — tools=[] (model cannot call a tool →
    must finish) and tool_choice="auto" (NOT the Gemini-unsafe "none")."""
    llm = _CapturingLLM(_finish())
    loop = _loop(llm)
    await loop._force_close_call(
        [{"role": "system", "content": "sp"}, {"role": "user", "content": "hi"}],
        resolved_model="gpt-4o-mini",
    )
    sent = llm.calls[-1]
    assert sent["tools"] == []
    assert sent["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_force_close_returns_finish_result() -> None:
    """Tier 2: the call returns the model's finish result (the consolidation)."""
    llm = _CapturingLLM(_finish("HANDOFF"))
    loop = _loop(llm)
    result = await loop._force_close_call(
        [{"role": "system", "content": "sp"}, {"role": "user", "content": "hi"}],
        resolved_model="gpt-4o-mini",
    )
    assert result.finish_reason == "stop"
    assert result.content == "HANDOFF"


@pytest.mark.asyncio
async def test_force_close_drops_extra_system_turns() -> None:
    """Tier 2: only the wrap-up SP is the system context — any pre-existing
    system turn(s) are dropped (robust to non-[0] system placement)."""
    llm = _CapturingLLM(_finish())
    loop = _loop(llm)
    messages = [
        {"role": "system", "content": "sp1"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "sp2 injected"},
    ]
    await loop._force_close_call(messages, resolved_model="gpt-4o-mini")
    sent = llm.calls[-1]["messages"]
    systems = [m for m in sent if m.get("role") == "system"]
    # Exactly one system turn — the wrap-up SP — and no other (sp1/sp2 dropped).
    assert systems == [{"role": "system", "content": wrap_up_system_prompt()}]
