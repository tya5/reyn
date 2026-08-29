"""Tier 1/2: #5470 — ``LLMStub``'s ``tool_call_for=``/``tool=``/``args=``
mode, the mechanism witnesses (architect's own #5470 design, witnesses
①②③④⑤⑥).

Real ``litellm.acompletion`` boundary throughout — ``LLMStub.install()``
patches it for real; no mock. The one production-level test
(``test_a_real_turn_dispatches_the_tool_call_and_terminates``) additionally
drives a REAL ``Session``/``RouterLoop``/``HookBus`` — architect's own
driving reason for this axis ("stub が production の本線を表せていない",
tool calls are the agent loop's MAIN path, not an exceptional one), so this
file does not defer that witness to a future per-file migration the way
``test_llm_stub_control_5450.py`` defers its witness ② — #5470 IS the
"does a real tool call reach a real production op" question.
"""
from __future__ import annotations

import pytest

from reyn.dev.testing.llm_stub import LLMStub
from tests._support.agent_session import make_session
from tests._support.hooks import collect_hook_events, run_one_turn


def _no_tool_result_yet(messages: "list[dict]") -> bool:
    """Content-only predicate (architect's acceptance condition — never a
    call count): True until the tool's own ``role="tool"`` result message
    is present, which the REAL router loop appends after dispatching the
    first tool call — so the SECOND completion call naturally sees this as
    False and the turn terminates instead of looping forever."""
    return not any(m.get("role") == "tool" for m in messages)


# ── witness ① — selective: predicate true -> tool call, false -> unchanged ──


@pytest.mark.asyncio
async def test_a_matching_call_returns_a_tool_calls_completion() -> None:
    """Tier 1: witness ① (true branch) — a tool_call_for=True call returns
    a completion whose message.tool_calls names `tool` with `args` as its
    JSON-encoded arguments."""
    stub = LLMStub(
        tool_call_for=lambda messages: True,
        tool="emit_hook_event",
        args={"event_name": "ping"},
    )
    stub.install()
    try:
        response = await stub._handle("m", [{"role": "user", "content": "hi"}])
    finally:
        stub.restore()

    choice = response.choices[0]
    assert choice.finish_reason == "tool_calls"
    (call,) = choice.message.tool_calls
    assert call.function.name == "emit_hook_event"
    import json

    assert json.loads(call.function.arguments) == {"event_name": "ping"}


@pytest.mark.asyncio
async def test_a_non_matching_call_keeps_the_ordinary_stop_response() -> None:
    """Tier 1: witness ① (false branch) — a tool_call_for=False call is
    UNCHANGED from #5103's original behavior (finish_reason=stop, no
    tool_calls) — the axis's absence-of-effect is as load-bearing as its
    presence."""
    stub = LLMStub(
        tool_call_for=lambda messages: False,
        tool="emit_hook_event",
    )
    stub.install()
    try:
        response = await stub._handle("m", [{"role": "user", "content": "hi"}])
    finally:
        stub.restore()

    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.tool_calls is None


# ── witness ② — the predicate discriminates a 2-call sequence, isolated ─────


@pytest.mark.asyncio
async def test_the_predicate_flips_once_a_tool_result_is_in_messages() -> None:
    """Tier 1: witness ② at the stub level (isolated from a real loop) —
    the SAME predicate a real router loop would drive against sees call 1
    (no tool result yet) as True and call 2 (tool result appended, the
    shape the real loop produces after dispatching) as False. This is the
    mechanism the production-level test below relies on to prove the turn
    actually terminates rather than looping forever.

    The ``role="tool"`` message below is hand-built (a plausible SHAPE, not
    a pin) — the production test below is what actually pins the REAL
    shape the real router loop produces; this test's own subject is only
    whether the predicate correctly discriminates "has a tool result" from
    "doesn't", not the exact wire format of that result."""
    stub = LLMStub(tool_call_for=_no_tool_result_yet, tool="emit_hook_event")
    stub.install()
    try:
        first = await stub._handle("m", [{"role": "user", "content": "hi"}])
        assert first.choices[0].finish_reason == "tool_calls"

        second = await stub._handle("m", [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
            {"role": "tool", "tool_call_id": "x", "content": '{"status": "ok"}'},
        ])
        assert second.choices[0].finish_reason == "stop"
    finally:
        stub.restore()


# ── witness ④ — closed vocabulary, checked at construction ─────────────────


def test_an_unregistered_tool_name_raises_at_construction() -> None:
    """Tier 1: witness ④ — `tool` is validated EAGERLY against
    `reyn.core.op_runtime.available_kinds()` (unlike `cause`, validated
    lazily — see module docstring for why). A typo must fail loud at
    construction, not silently walk the router's own unknown-tool error
    path and read as a passing green."""
    with pytest.raises(ValueError, match="not a registered op kind"):
        LLMStub(tool_call_for=lambda messages: True, tool="not_a_real_op_kind")


def test_tool_call_for_and_tool_must_be_given_together() -> None:
    """Tier 1: pairing guard, same shape as raise_for/cause's own."""
    with pytest.raises(ValueError, match="must be given together"):
        LLMStub(tool_call_for=lambda messages: True)
    with pytest.raises(ValueError, match="must be given together"):
        LLMStub(tool="emit_hook_event")


def test_args_without_tool_call_for_is_rejected() -> None:
    """Tier 1: `args=` is meaningless without `tool_call_for`/`tool` —
    reject rather than silently ignore it."""
    with pytest.raises(ValueError, match="without tool_call_for"):
        LLMStub(args={"event_name": "ping"})  # type: ignore[call-arg]


# ── witness ⑤ — accept-side / noise guard ───────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_for_absent_is_unchanged_from_before_5470() -> None:
    """Tier 1: accept-side / noise guard — the default (no tool_call_for=)
    keeps every prior axis's behavior unaffected. Every existing
    @llm_stub-marked file (raise_for/control/plain) depends on this
    staying true — see test_llm_stub_5103.py / test_llm_stub_control_5450.py
    / test_5382_llm_stub_compaction_selectivity.py, all still green
    alongside this file."""
    stub = LLMStub()
    stub.install()
    try:
        response = await stub._handle("m", [{"role": "user", "content": "hi"}])
    finally:
        stub.restore()

    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.tool_calls is None


# ── witnesses ②③ (production) — a REAL turn, REAL dispatch, REAL HookBus ───


@pytest.mark.asyncio
async def test_a_real_turn_dispatches_the_tool_call_and_terminates() -> None:
    """Tier 2: #5470's own driving reason, witnessed directly — a REAL
    Session/RouterLoop, patched ONLY at the litellm.acompletion boundary,
    dispatches the stubbed tool call through the REAL tool/op-runtime
    plumbing (`reyn.tools.emit_hook_event` -> `op_runtime.emit_hook_event.
    handle`) onto a REAL HookBus (witness ③ — this issue's own subject),
    and the turn terminates after exactly one round-trip instead of
    looping forever (witness ②) — proven by the SAME content-only
    predicate `test_the_predicate_flips_once_a_tool_result_is_in_messages`
    exercises in isolation above, now driven by the real loop's own
    message assembly rather than a hand-built one.

    STRIP-FALSIFY (documented, performed by hand): commenting out the
    `tool_call_for` branch in `LLMStub._handle` makes this call return the
    ordinary empty-stop response instead — the router loop then sees
    "the model said nothing", the turn completes with zero tool calls, and
    `sub.get_nowait()` raises `asyncio.QueueEmpty` (RED), proving this
    assertion is load-bearing on the branch actually running, not a
    tautology.

    #5494: this test's own private reach into `session._hook_bus.
    subscribe()` and `session._run_router_loop(...)` — the exact hole
    architect found while reviewing this file — is now closed via
    `tests/_support/hooks.py`'s `collect_hook_events`/`run_one_turn`;
    migrated here as that fix's own first real consumer."""
    session = make_session(agent_name="tool-call-stub-witness")
    sub = collect_hook_events(session)

    stub = LLMStub(
        tool_call_for=_no_tool_result_yet,
        tool="emit_hook_event",
        args={"event_name": "ping"},
    )
    stub.install()
    try:
        await run_one_turn(session, "hello", "tool-call-chain")
    finally:
        stub.restore()

    event = sub.get_nowait()
    assert event.kind == f"llm:{session.session_id}:ping"
