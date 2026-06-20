"""Tier 2: OS invariant — force-close trigger seam (#1092 PR-C).

The per-turn layer-1 trigger wires ``should_force_close`` into ``run_loop`` right
after the compaction hook. When a host signals force-close, the normal act-turn
LLM call is SWAPPED for the wrap-up (force-close) call, whose finish the loop's
terminal path consumes. These pin:

- trigger fires → the force-close call is used (``trace_caller="router_force_close"``,
  ``tools=[]``), not the normal act-turn call;
- LOOP-FREE: the force-close result is a finish (no tool_calls) → the turn ENDS;
  the trigger is consulted once and the loop does not churn / re-trigger;
- no signal → the normal call is used;
- a host WITHOUT ``should_force_close`` (= chat) → byte-identical normal call
  (the seam is inert);
- a ``force_close_triggered`` event is emitted (P6 audit).

No mocks: a real ``RouterLoop`` + ``FakeRouterHost`` subclass + a real capturing
LLM callable.
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from tests._support.router_loop import FakeRouterHost


class _ForceCloseHost(FakeRouterHost):
    """FakeRouterHost + a ``should_force_close`` hook returning a fixed decision,
    counting how often it is consulted (to prove loop-free = consulted once)."""

    def __init__(self, *, force_close: bool, **kw: Any) -> None:
        super().__init__(**kw)
        self._force_close = force_close
        self.should_force_close_calls = 0

    async def should_force_close(self, messages: list[dict], *, model: str) -> bool:
        self.should_force_close_calls += 1
        return self._force_close


class _CapturingFinishLLM:
    """Real callable: records each call's kwargs + returns a finish (no tools)."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_kwargs: dict = {}

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.call_count += 1
        self.last_kwargs = kwargs
        return LLMToolCallResult(
            content="consolidated handoff", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=40, completion_tokens=8),
        )


def _loop(host: FakeRouterHost, llm: _CapturingFinishLLM) -> RouterLoop:
    return RouterLoop(host=host, chain_id="chain-trig", max_iterations=5,
                      llm_caller=llm)


@pytest.mark.asyncio
async def test_trigger_fires_force_close_call() -> None:
    """Tier 2: when the host signals force-close, the act-turn call is the wrap-up
    call (trace_caller=router_force_close, tools=[]), not the normal act turn."""
    host = _ForceCloseHost(force_close=True)
    llm = _CapturingFinishLLM()
    await _loop(host, llm).run("do the task", [])
    assert llm.last_kwargs["trace_caller"] == "router_force_close"
    assert llm.last_kwargs["tools"] == []


@pytest.mark.asyncio
async def test_force_close_is_terminal_loop_free() -> None:
    """Tier 2: (★loop-free) the force-close finish (no tool_calls) ENDS the turn —
    the trigger is consulted once and the loop neither re-triggers nor reverts to
    a normal call (no threshold→shrink→regrow churn within the loop)."""
    host = _ForceCloseHost(force_close=True)
    llm = _CapturingFinishLLM()
    await _loop(host, llm).run("do the task", [])
    assert llm.last_kwargs["trace_caller"] == "router_force_close"  # the FC path
    assert host.should_force_close_calls == 1   # consulted once, then terminal
    assert llm.call_count == 1                   # one force-close call, no churn


@pytest.mark.asyncio
async def test_no_trigger_uses_normal_call() -> None:
    """Tier 2: when the host does NOT signal force-close, the normal act-turn call
    is used (trace_caller=router), not the wrap-up call."""
    host = _ForceCloseHost(force_close=False)
    llm = _CapturingFinishLLM()
    await _loop(host, llm).run("do the task", [])
    assert llm.last_kwargs["trace_caller"] == "router"


@pytest.mark.asyncio
async def test_chat_host_without_hook_uses_normal_path() -> None:
    """Tier 2: a host WITHOUT should_force_close (= chat) takes the normal path
    unchanged — the seam is inert (getattr-guarded)."""
    host = FakeRouterHost()  # no should_force_close
    llm = _CapturingFinishLLM()
    await _loop(host, llm).run("do the task", [])
    assert llm.last_kwargs["trace_caller"] == "router"


@pytest.mark.asyncio
async def test_force_close_emits_audit_event() -> None:
    """Tier 2: (P6) a force_close_triggered event is emitted when the trigger fires."""
    host = _ForceCloseHost(force_close=True)
    llm = _CapturingFinishLLM()
    await _loop(host, llm).run("do the task", [])
    emitted = [e["type"] for e in host._events.emitted]
    assert "force_close_triggered" in emitted
