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

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from reyn.services.turn_budget import wrap_up_system_prompt
from tests._support.router_loop import FakeRouterHost


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


# ── layer-2 force-close retry (#1092 PR-B 2/2) ───────────────────────────────
#
# #4978: the shrink-and-retry branch these tests used to exercise
# (`maybe_compact_messages`, a "PHASE host" getattr-optional hook) is
# removed — 0 production hosts ever implemented it (the only
# implementation was this file's own `_ShrinkHost` test double, now
# deleted along with it). `_force_close_call_with_retry` no longer
# branches on host capability at all: EVERY host now takes the
# immediate re-raise path the tests below were already covering for
# the "no shrink hook" case — that behavior survives unchanged; the
# shrink-recovery and floor-abort tests that only exercised the dead
# branch do not (removed, not adapted — see #4978's own PR body for
# the per-assert accounting this section's tests underwent).


class _OverflowThenFinishLLM:
    """Real callable: raises a context-overflow-looking error ``overflow_count``
    times, then returns ``result``. Records each call."""

    def __init__(self, overflow_count: int, result: LLMToolCallResult) -> None:
        self._remaining = overflow_count
        self._result = result
        self.call_count: int = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.call_count += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("litellm: this model's maximum context length / too large")
        return self._result


def _retry_loop(host: FakeRouterHost, llm) -> RouterLoop:
    return RouterLoop(host=host, chain_id="chain-fc-retry", max_iterations=5,
                      llm_caller=llm)


def _msgs_with_tools(n: int) -> list[dict]:
    base = [{"role": "system", "content": "sp"}, {"role": "user", "content": "u"}]
    return base + [
        {"role": "tool", "content": f"result {i}", "tool_call_id": f"t{i}"}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_force_close_retry_reraises_overflow_immediately() -> None:
    """Tier 2: a force-close call that overflows re-raises immediately —
    it propagates to the session's outer retry_loop, no in-loop retry
    (#4978: this is now the ONLY behavior, not one side of a host-capability
    axis — see this section's own header comment)."""
    llm = _OverflowThenFinishLLM(overflow_count=1, result=_finish())
    loop = _retry_loop(FakeRouterHost(), llm)
    with pytest.raises(RuntimeError):
        await loop._force_close_call_with_retry(
            _msgs_with_tools(2), resolved_model="gpt-4o-mini"
        )
    assert llm.call_count == 1  # no retry — straight to the outer loop


@pytest.mark.asyncio
async def test_non_overflow_error_is_also_reraised_immediately() -> None:
    """Tier 2: a non-overflow exception is re-raised immediately too — same
    single-call, no-retry behavior regardless of the exception's shape."""
    class _BoomLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
            self.calls += 1
            raise ValueError("unrelated boom")

    llm = _BoomLLM()
    loop = _retry_loop(FakeRouterHost(), llm)
    with pytest.raises(ValueError):
        await loop._force_close_call_with_retry(
            _msgs_with_tools(3), resolved_model="gpt-4o-mini"
        )
    assert llm.calls == 1  # no retry
