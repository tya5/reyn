"""#1652: reasoning_content capture at the LLM boundary (always-on).

Capture is unconditional (independent of the cross-turn-continuity and UI-display
toggles, which gate persist/replay/display, not capture). This pins that the
provider's reasoning text is surfaced on LLMToolCallResult.reasoning so the chat
layer can persist + optionally replay it.

The persist / replay / display / config-toggle tests land with that wiring once
the toggle schema is locked (held on owner confirm) — this file currently covers
only the settled capture boundary.

No mocks: a real async callable stub at the litellm boundary (testing policy).
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.llm.model_resolver import ModelSpec

_REASONING = "REYN_1652_THOUGHTS: 17*23 = 17*20 + 17*3 = 340 + 51 = 391."


def _resp(*, content: str | None, reasoning: str | None):
    msg = type(
        "_Msg", (), {"content": content, "tool_calls": None, "reasoning_content": reasoning}
    )()
    choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
    usage = type("_Usage", (), {"prompt_tokens": 10, "completion_tokens": 5})()
    return type("_Resp", (), {"choices": [choice], "usage": usage})()


class _StubLLM:
    def __init__(self, *, content, reasoning):
        self._content = content
        self._reasoning = reasoning

    async def __call__(self, **kwargs: Any):
        return _resp(content=self._content, reasoning=self._reasoning)


@pytest.mark.asyncio
async def test_reasoning_content_captured_onto_result(monkeypatch):
    """Tier 2: #1652 — the provider reasoning_content is surfaced on
    LLMToolCallResult.reasoning, distinct from the visible content."""
    import litellm

    from reyn.llm.llm import call_llm_tools

    monkeypatch.setattr(litellm, "acompletion", _StubLLM(content="391", reasoning=_REASONING))
    r = await call_llm_tools(
        model=ModelSpec(model="gemini/gemini-2.5-flash-lite", kwargs={}),
        messages=[{"role": "user", "content": "17*23?"}],
        tools=[],
        max_retries=0,
    )
    assert r.reasoning == _REASONING
    assert r.content == "391"  # visible content unchanged, kept separate


@pytest.mark.asyncio
async def test_no_reasoning_normalises_to_none(monkeypatch):
    """Tier 2: #1652 — a response with no/empty reasoning_content yields
    reasoning=None (not ""), so 'has thoughts' is a clean truthiness check."""
    import litellm

    from reyn.llm.llm import call_llm_tools

    monkeypatch.setattr(litellm, "acompletion", _StubLLM(content="hi", reasoning=""))
    r = await call_llm_tools(
        model=ModelSpec(model="gemini/gemini-2.5-flash-lite", kwargs={}),
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_retries=0,
    )
    assert r.reasoning is None
