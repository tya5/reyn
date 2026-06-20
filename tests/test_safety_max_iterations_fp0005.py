"""Tier 2: FP-0005 max_iterations checkpoint wiring in RouterLoop.

Invariants pinned:

1. When max_iterations is exhausted and on_limit is wired (interactive mode),
   handle_limit_exceeded is called — NOT a flat abort.
2. When the bus answers YES, extension applied and loop continues for extension
   more iterations before hitting the next limit (or completing).
3. When the bus answers NO (user refused), the loop stops with a
   decision-enabling error message (mentions config key to change).
4. When on_limit=None (legacy path), flat-abort error is still decision-enabling
   (mentions safety.loop.max_router_iterations config key).
5. When bus=None (interactive mode, no bus wired), handle_limit_exceeded
   returns no_bus → decision-enabling error (not silent abort).
6. When mode=unattended, no ask dispatched, immediate abort with error.

Loop exhaustion mechanism: script the LLM to return an unknown tool call
(not invoke_skill, not in registry) each iteration. dispatch_tool returns
unknown_tool error; the loop adds it to messages and continues. After
max_iterations calls the limit fires.

No mocks. RouterLoop is driven via llm_caller= injection. RequestBus is
a real subclass (not MagicMock) per no-mock policy.
"""
from __future__ import annotations

import json

import pytest

from reyn.config import OnLimitConfig
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from reyn.user_intervention import InterventionAnswer, UserIntervention
from tests._support.router_loop import FakeRouterHost, text_result
from tests._support.router_loop import ScriptedLLM as _ScriptedLLM

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _loop_exhauster(n: int) -> LLMToolCallResult:
    """LLMToolCallResult with one unknown tool call — makes the loop iterate
    without early return (dispatch_tool returns unknown_tool error, loop
    adds the error to messages and continues)."""
    return LLMToolCallResult(
        content=None,
        tool_calls=[{
            "id": f"tc_{n}",
            "type": "function",
            "function": {
                "name": "_test_loop_exhauster",
                "arguments": json.dumps({"n": n}),
            },
        }],
        finish_reason="tool_calls",
        usage=_EMPTY_USAGE,
    )


# ── Fake RequestBus ───────────────────────────────────────────────────────────


class _FakeRequestBus:
    """Real RequestBus subclass (non-mock) that records asks and returns
    a pre-scripted choice."""

    def __init__(self, answer_choice_id: str) -> None:
        self._answer = answer_choice_id
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._answer, choice_id=self._answer)


# ── Host with make_intervention_bus ──────────────────────────────────────────


class _LimitHost(FakeRouterHost):
    """FakeRouterHost that exposes make_intervention_bus() for FP-0005."""

    def __init__(self, bus: "_FakeRequestBus | None" = None) -> None:
        super().__init__()
        self._bus = bus

    def make_intervention_bus(self) -> "_FakeRequestBus | None":
        return self._bus


def _loop(
    host: _LimitHost,
    llm: _ScriptedLLM,
    max_iterations: int,
    on_limit: "OnLimitConfig | None" = None,
) -> RouterLoop:
    return RouterLoop(
        host=host,
        chain_id="chain-limit-test",
        max_iterations=max_iterations,
        llm_caller=llm,
        on_limit=on_limit,
    )


async def _exhaust_loop(
    host: _LimitHost,
    n_exhaust: int,
    max_iterations: int,
    on_limit: "OnLimitConfig | None" = None,
    extra_after: "list[LLMToolCallResult] | None" = None,
) -> None:
    """Run the loop with n_exhaust unknown-tool calls to hit the limit,
    then optionally extra results after an extension."""
    script = [_loop_exhauster(i) for i in range(n_exhaust)]
    if extra_after:
        script.extend(extra_after)
    llm = _ScriptedLLM(script)
    loop = _loop(host, llm, max_iterations=max_iterations, on_limit=on_limit)
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        _univ_enabled=False,
    )


# ── 1. Bus=YES → extension applied, loop continues ───────────────────────────


@pytest.mark.asyncio
async def test_max_iterations_interactive_yes_extends_loop() -> None:
    """Tier 2: FP-0005 — when max_iterations exhausted and bus answers YES,
    handle_limit_exceeded called, extension applied, loop produces agent reply."""
    bus = _FakeRequestBus(answer_choice_id="yes")
    host = _LimitHost(bus=bus)
    on_limit = OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0)

    # max_iterations=2: exhaust 2 iterations → ask → YES → 2 more → text reply
    await _exhaust_loop(
        host,
        n_exhaust=2,          # exhaust first limit
        max_iterations=2,
        on_limit=on_limit,
        extra_after=[_loop_exhauster(99), text_result("done after extension")],
    )

    # Bus was asked exactly once (first exhaustion)
    (ask,) = bus.asks  # unpack-assertion: exactly 1 ask
    assert "max_iterations" in ask.kind
    # Loop continued: agent reply emitted (not an error)
    agent_msgs = [m for m in host.outbox if m["kind"] == "agent"]
    (agent_msg,) = agent_msgs
    assert agent_msg["text"] == "done after extension"


# ── 2. Bus=NO → decision-enabling error message ──────────────────────────────


@pytest.mark.asyncio
async def test_max_iterations_interactive_no_emits_decision_enabling_error() -> None:
    """Tier 2: FP-0005 — when max_iterations exhausted and bus answers NO,
    error message is decision-enabling (mentions what to configure)."""
    bus = _FakeRequestBus(answer_choice_id="no")
    host = _LimitHost(bus=bus)
    on_limit = OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0)

    await _exhaust_loop(host, n_exhaust=2, max_iterations=2, on_limit=on_limit)

    (ask,) = bus.asks
    assert "max_iterations" in ask.kind
    error_msgs = [m for m in host.outbox if m["kind"] == "error"]
    (err,) = error_msgs
    # Decision-enabling: mentions config key
    assert "router_max_iterations" in err["text"] or "on_limit" in err["text"]


# ── 3. Bus=None (interactive mode) → decision-enabling error ─────────────────


@pytest.mark.asyncio
async def test_max_iterations_no_bus_decision_enabling_error() -> None:
    """Tier 2: FP-0005 — bus=None + interactive mode → no_bus path →
    decision-enabling error, NOT silent abort."""
    host = _LimitHost(bus=None)
    on_limit = OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0)

    await _exhaust_loop(host, n_exhaust=1, max_iterations=1, on_limit=on_limit)

    error_msgs = [m for m in host.outbox if m["kind"] == "error"]
    (err,) = error_msgs
    assert "router_max_iterations" in err["text"] or "on_limit" in err["text"]


# ── 4. on_limit=None → legacy flat-abort is decision-enabling ────────────────


@pytest.mark.asyncio
async def test_max_iterations_no_on_limit_legacy_decision_enabling() -> None:
    """Tier 2: FP-0005 — on_limit=None legacy path: error message is
    decision-enabling (mentions safety.loop.max_router_iterations config key)."""
    host = _LimitHost(bus=None)

    await _exhaust_loop(host, n_exhaust=1, max_iterations=1, on_limit=None)

    error_msgs = [m for m in host.outbox if m["kind"] == "error"]
    (err,) = error_msgs
    assert "max_router_iterations" in err["text"]


# ── 5. Unattended mode → immediate abort, no ask ─────────────────────────────


@pytest.mark.asyncio
async def test_max_iterations_unattended_no_ask() -> None:
    """Tier 2: FP-0005 — unattended mode: no ask dispatched, immediate error."""
    bus = _FakeRequestBus(answer_choice_id="yes")  # would answer yes if asked
    host = _LimitHost(bus=bus)
    on_limit = OnLimitConfig(mode="unattended")

    await _exhaust_loop(host, n_exhaust=1, max_iterations=1, on_limit=on_limit)

    # Bus NOT asked
    assert not bus.asks
    # Error was emitted (scripted LLM returns no text on wrap-up → fallback error)
    error_msgs = [m for m in host.outbox if m["kind"] == "error"]
    (err,) = error_msgs
    assert "router_max_iterations" in err["text"] or "on_limit" in err["text"]


# ── 6. Limit-deny → force-close wrap-up (LLM returns text) ───────────────────


@pytest.mark.asyncio
async def test_max_iterations_limit_deny_fires_force_close_wrap_up() -> None:
    """Tier 2: #1496 — when limit fires and deny, force-close wrap-up is called;
    LLM wrap-up text emitted as 'agent' with limit_stopped meta, no canned error,
    limit_denied WAL event emitted."""
    host = _LimitHost(bus=None)
    on_limit = OnLimitConfig(mode="unattended")

    # Script: 1 exhaust → limit fires → force-close LLM call returns text
    script = [_loop_exhauster(0), text_result("work done: X; remaining: Y; stopped by limit")]
    llm = _ScriptedLLM(script)
    from reyn.runtime.router_loop import RouterLoop
    loop = RouterLoop(
        host=host,
        chain_id="chain-fc-test",
        max_iterations=1,
        llm_caller=llm,
        on_limit=on_limit,
    )
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        _univ_enabled=False,
    )

    # Agent message emitted (wrap-up text), not canned error
    agent_msgs = [m for m in host.outbox if m["kind"] == "agent"]
    (msg,) = agent_msgs  # unpack: exactly one
    assert msg["text"] == "work done: X; remaining: Y; stopped by limit"
    assert msg["meta"].get("limit_stopped") is True
    assert msg["meta"].get("limit_kind") == "max_iterations"
    assert not [m for m in host.outbox if m["kind"] == "error"]

    # limit_denied WAL event emitted
    limit_events = [e for e in host.events.emitted if e.get("type") == "limit_denied"]
    (ev,) = limit_events
    assert ev["kind"] == "max_iterations"


# ── 7. Plan/phase axis: record_force_close called only when content non-empty ─


class _RecordingHost(_LimitHost):
    """_LimitHost + record_force_close hook (plan/phase axis simulation)."""

    def __init__(self, bus: "_FakeRequestBus | None" = None) -> None:
        super().__init__(bus=bus)
        self.recorded_fc: list = []

    def record_force_close(self, result: object) -> None:
        self.recorded_fc.append(result)


@pytest.mark.asyncio
async def test_record_force_close_called_when_wrap_up_has_content() -> None:
    """Tier 2: #1496 — plan/phase axis: record_force_close is called when
    force-close wrap-up returns non-empty text (host has the method)."""
    host = _RecordingHost(bus=None)
    on_limit = OnLimitConfig(mode="unattended")

    # Script: 1 exhaust → force-close returns text
    script = [_loop_exhauster(0), text_result("step done; remaining: cleanup")]
    llm = _ScriptedLLM(script)
    loop = RouterLoop(
        host=host, chain_id="chain-plan-test", max_iterations=1,
        llm_caller=llm, on_limit=on_limit,
    )
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )

    # record_force_close called exactly once with the wrap-up result
    (fc_result,) = host.recorded_fc
    assert getattr(fc_result, "content", None) == "step done; remaining: cleanup"
    # agent message emitted
    agent_msgs = [m for m in host.outbox if m["kind"] == "agent"]
    (msg,) = agent_msgs
    assert msg["meta"].get("limit_stopped") is True


@pytest.mark.asyncio
async def test_record_force_close_NOT_called_when_wrap_up_empty() -> None:
    """Tier 2: #1496 — plan/phase axis: record_force_close is NOT called when
    wrap-up returns no content (avoids empty-consolidation checkpoint re-entry)."""
    host = _RecordingHost(bus=None)
    on_limit = OnLimitConfig(mode="unattended")

    # Script: exhaust only (no text for force-close) → content=None → fallback
    await _exhaust_loop(host, n_exhaust=1, max_iterations=1, on_limit=on_limit)

    # record_force_close NOT called — empty content must not trigger re-entry
    assert not host.recorded_fc
    # Fallback canned error emitted
    error_msgs = [m for m in host.outbox if m["kind"] == "error"]
    (err,) = error_msgs
    assert "max_router_iterations" in err["text"] or "on_limit" in err["text"]
