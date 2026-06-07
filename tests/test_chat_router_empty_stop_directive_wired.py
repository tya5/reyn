"""Tier 2: ChatSession wires the empty-stop retry into the chat router's
RouterLoop — now the SHARED uniform ``"resume"`` directive, always-on (#187).

Pinned invariants:

- ``ChatSession._handle_user_message`` constructs ``RouterLoop`` with
  ``empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE`` (the single shared
  "resume" directive — NOT a chat-specific string) AND
  ``empty_stop_retry_auto=True`` (always-on; the ``REYN_EMPTY_STOP_RETRY`` env
  opt-in is retired). Pinned via a real ``CapturingRouterLoop`` subclass
  injected through ``pytest.monkeypatch`` (= module-attribute setup, not
  fake-collaborator mocking per testing.ja.md).

History (#187 owner decision, 2026-06-07): the previous chat-specific directive
(B43-NF-W6-1: "Now write your reply to the user … Do not call another tool.")
was RETIRED. It was unevidenced per-site differentiation, and its anti-invoke
framing ("do not call another tool") was itself suspect — chat models also
tool-call. All sites (chat / plan-step / agent op-loop) now use the single
content-neutral ``EMPTY_STOP_RETRY_DIRECTIVE`` = "resume". The cross-site
uniform-wiring invariant is pinned in
``test_empty_stop_retry_uniform_187``; this file pins the chat site's wiring
behaviourally (driving the real ChatSession construction).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.chat import router_loop as rl
from reyn.chat.router_loop import EMPTY_STOP_RETRY_DIRECTIVE
from reyn.chat.session import ChatSession
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


class _ScriptedLLM:
    """Real-fake callable matching the ``call_llm_tools`` signature."""

    def __init__(self, script: list[LLMToolCallResult]):
        self._script = list(script)
        self.call_count = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        result = self._script[self.call_count]
        self.call_count += 1
        return result


def _make_session(tmp_path: Path) -> ChatSession:
    return ChatSession(agent_name="test_agent_b44")


# ---------------------------------------------------------------------------
# Wiring pin — ChatSession constructs RouterLoop with the shared directive
# ---------------------------------------------------------------------------


class _CapturingRouterLoop(rl.RouterLoop):
    """Real subclass that records the kwargs every RouterLoop construction
    receives. Used in place of the production RouterLoop via
    ``monkeypatch.setattr`` so the test observes ChatSession's construction
    call without mocking the type contract."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any):
        _CapturingRouterLoop.last_kwargs = dict(kwargs)
        super().__init__(**kwargs)


@pytest.mark.asyncio
async def test_chat_session_passes_shared_resume_directive_always_on(monkeypatch, tmp_path):
    """Tier 2c: ``ChatSession._handle_user_message`` wires the SHARED
    ``EMPTY_STOP_RETRY_DIRECTIVE`` ("resume") + ``empty_stop_retry_auto=True``
    to ``RouterLoop`` (#187 uniform always-on).

    Without this wiring the chat router would either miss the retry or carry a
    chat-specific directive (the retired per-site differentiation). Pin both the
    kwarg's presence AND its identity against the shared module-level constant,
    plus the always-on flag (env-gate retired)."""
    monkeypatch.chdir(tmp_path)
    # session._handle_user_message does ``from reyn.chat.router_loop import
    # RouterLoop`` inside the function, so the module-level patch is observed.
    monkeypatch.setattr(rl, "RouterLoop", _CapturingRouterLoop)
    scripted = _ScriptedLLM([_text_result("ok")])
    monkeypatch.setattr(rl, "call_llm_tools", scripted)

    session = _make_session(tmp_path)
    session.is_attached = True

    await session._handle_user_message("hello", chain_id="chain-test-b44")

    captured = _CapturingRouterLoop.last_kwargs
    assert captured.get("empty_stop_retry_directive") == EMPTY_STOP_RETRY_DIRECTIVE, (
        "ChatSession must pass the shared EMPTY_STOP_RETRY_DIRECTIVE, not an "
        "inlined or chat-specific string. Got: "
        + repr(captured.get("empty_stop_retry_directive"))
    )
    assert captured.get("empty_stop_retry_auto") is True, (
        "ChatSession must pass empty_stop_retry_auto=True (#187 always-on; the "
        "REYN_EMPTY_STOP_RETRY env opt-in is retired). Got kwargs: "
        + str(sorted(captured))
    )
