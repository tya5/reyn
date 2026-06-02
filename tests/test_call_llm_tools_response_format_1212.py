"""Tier 2: #1212 PR2 — call_llm_tools opt-in response_format degrades via the cache.

The op-loop call (D4) specifies tools + response_format uniformly. On a provider
that rejects the combination (Gemini 400), reyn's broad fallback drops
response_format and retries tools-only, and the PR1 capability cache records the
(model, has_tools=True) shape so the next call skips the doomed attempt. With
response_format=None (the chat default), call_llm_tools is unchanged.

Real call_llm_tools + real capability cache; the only scripted seam is
litellm.acompletion (the external provider boundary, the sanctioned pattern in
test_cost_chokepoint_1190 / test_capability_cache_1212), not a collaborator mock.
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from reyn.llm import capability_cache as cc
from reyn.llm.llm import call_llm_tools

_TOOLS = [{
    "type": "function",
    "function": {
        "name": "file__read",
        "description": "read",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}]


@pytest.fixture(autouse=True)
def _reset_cache():
    cc.reset()
    yield
    cc.reset()


class _Msg:
    tool_calls = None
    content = "done"


class _Choice:
    message = _Msg()
    finish_reason = "stop"


class _Resp:
    choices = [_Choice()]
    usage = None


def test_response_format_degrades_and_caches(monkeypatch) -> None:
    """Tier 2: combined tools+response_format → 400 → tools-only retry succeeds,
    and (model, has_tools=True) is cached unsupported."""
    calls: list[bool] = []

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        has_rf = "response_format" in kw
        calls.append(has_rf)
        if has_rf:
            raise ValueError("combined tools+response_format unsupported (Gemini 400)")
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake)

    res = asyncio.run(call_llm_tools(
        model="m", messages=[{"role": "user", "content": "x"}],
        tools=_TOOLS, response_format={"type": "json_object"},
    ))
    assert res is not None
    assert calls == [True, False], "first attempt with rf 400s → retry tools-only"
    assert cc.response_format_supported("m", has_tools=True) is False


def test_no_response_format_is_chat_default_no_cache(monkeypatch) -> None:
    """Tier 2: response_format=None (chat default) → no rf attempt, cache untouched."""
    calls: list[bool] = []

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        calls.append("response_format" in kw)
        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake)

    asyncio.run(call_llm_tools(
        model="m2", messages=[{"role": "user", "content": "x"}], tools=_TOOLS,
    ))
    assert calls == [False], "no response_format ever sent"
    assert cc.response_format_supported("m2", has_tools=True) is None
