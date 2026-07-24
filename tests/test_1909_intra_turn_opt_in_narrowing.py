"""Tier 2: opt-in intra-turn untrusted-content capability narrowing (#1909).

#3187 landed the ``external_source`` taint propagation for tool-results into
history ``meta`` (the S4/#1827 marker convention), but ``RouterLoop.
_contextual_permission`` is resolved ONCE per turn (``RouterLoopDriver.
run_turn`` calls ``contextual_for_turn_fn()`` once, before constructing
``RouterLoop``) and stays frozen for every LLM/tool round WITHIN that turn's
``run()``. So an external tool-result spliced in round *N* of a turn does NOT
narrow round *N+1*'s dispatch in the SAME turn — the same-turn injection
window this issue names.

Owner directive: UX/predictability > security — security is OPT-IN. Default
behavior (``safety.loop.intra_turn_untrusted_narrowing=False``) must stay
BYTE-IDENTICAL to today (turn-boundary narrowing only). Only when an operator
opts in does the mid-turn narrowing engage, with a turn-scoped MONOTONIC
LATCH: ``_effective_contextual_for_turn`` self-clears its taint once the
tainted history entry compacts out (until-compaction scope) — a naive
per-iteration re-scan would let an injected-content-triggered mid-turn
compaction evict the marker and let capabilities RECOVER within the same
turn (a taint-laundering hole). The latch keeps narrowing engaged through
turn end regardless of a later compaction.

Real ``Session`` + real history + real tool registry + scripted
``call_llm_tools`` throughout (mirrors ``test_1909_external_tool_result_
narrowing.py`` / ``test_context_auto_1827_s4b.py``) — no ``_FakeMessage`` /
hand-rolled host stand-in (#2957 PR-A precedent).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from reyn.config.chat import LoopConfig, SafetyConfig
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)

_REMEMBER_ARGS = {
    "slug": "y", "name": "n", "description": "d", "type": "user", "body": "x",
}


def _tool_call_result(calls: list[dict]) -> LLMToolCallResult:
    tool_calls = [
        {
            "id": c.get("id", f"tc_{i}"),
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c.get("args", {}))},
        }
        for i, c in enumerate(calls)
    ]
    return LLMToolCallResult(
        content=None, tool_calls=tool_calls, finish_reason="tool_calls", usage=_USAGE,
    )


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(content=text, tool_calls=[], finish_reason="stop", usage=_USAGE)


def _scripted_llm(rounds: list, on_round: "dict[int, Any] | None" = None):
    """Scripted ``call_llm_tools`` replacement. ``on_round`` maps a 0-based
    round index to a zero-arg callback invoked (as a side effect) exactly
    when that round is served — used to simulate a mid-turn compaction
    happening between two LLM rounds of the SAME turn."""
    state = {"n": 0}
    on_round = on_round or {}

    async def _call(**kwargs: Any) -> LLMToolCallResult:
        idx = state["n"]
        r = rounds[idx]
        state["n"] += 1
        cb = on_round.get(idx)
        if cb is not None:
            cb()
        return r
    return _call


def _make(safety: SafetyConfig | None = None) -> Session:
    return make_session(agent_name="test_agent", safety=safety or SafetyConfig())


@pytest.mark.asyncio
async def test_off_default_not_narrowed_mid_turn_same_turn(tmp_path, monkeypatch):
    """Tier 2: OFF (default) — a same-TURN external tool-result does NOT
    narrow a later round's dispatch in that same turn (today's behavior,
    preserved byte-identically). ``remember_shared`` (denied by the built-in
    ``_untrusted`` floor once narrowed) still succeeds in round 2 of the
    SAME ``_handle_user_message`` call that dispatched the external
    ``list_memory`` in round 1 — proving the off-path leaves
    ``RouterLoop._contextual_permission`` turn-frozen."""
    monkeypatch.chdir(tmp_path)
    session = _make()  # SafetyConfig() default: intra_turn_untrusted_narrowing=False
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([{"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"}]),
            _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_r"}]),
            _text_result("done"),
        ]),
    )
    await session._handle_user_message("look something up then remember it", chain_id="c1")

    (msg,) = [m for m in session.history if m.tool_call_id == "tc_r"]
    assert "tool_excluded" not in str(msg.content)


@pytest.mark.asyncio
async def test_on_engages_mid_turn_and_emits_audit_event(tmp_path, monkeypatch):
    """Tier 2: ON — the SAME single-turn sequence as the off-leg above, but
    with ``intra_turn_untrusted_narrowing=True``: round 2's ``remember_shared``
    dispatch (in the SAME ``_handle_user_message`` call, same turn, after
    round 1's external ``list_memory``) IS denied — narrowing engaged
    mid-turn. Also asserts the audit-event fires exactly once on the
    engage transition."""
    monkeypatch.chdir(tmp_path)
    session = _make(SafetyConfig(loop=LoopConfig(intra_turn_untrusted_narrowing=True)))
    captured = []
    session.subscribe_chat_events(
        lambda ev: captured.append(ev)
        if getattr(ev, "type", None) == "untrusted_narrowing_engaged"
        else None
    )
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([{"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"}]),
            _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_denied"}]),
            _text_result("done"),
        ]),
    )
    await session._handle_user_message("look something up then remember it", chain_id="c1")

    (denied_msg,) = [m for m in session.history if m.tool_call_id == "tc_denied"]
    assert "tool_excluded" in str(denied_msg.content)
    assert "remember_shared" in str(denied_msg.content)
    # Fires on the engage TRANSITION only (iteration 1, where the taint is
    # first observed) — not again on iteration 2's re-resolve, even though
    # that iteration is still tainted (already latched).
    assert [e.data.get("iteration") for e in captured] == [1]
    assert captured[0].data.get("provenance") == "external_source"


@pytest.mark.asyncio
async def test_on_latch_survives_mid_turn_compaction(tmp_path, monkeypatch):
    """Tier 2: ON + monotonic latch, compaction-immune (★ LOAD-BEARING).

    Round 1 dispatches the external ``list_memory`` (taints history) and the
    top-of-iteration-2 re-resolve observes the taint and LATCHES (first
    engage). Round 2 is a harmless trusted call (``list_agents``); its
    scripted side effect simulates a mid-turn compaction that EVICTS the
    ``external_source``-tagged history entry AFTER the latch already engaged
    but BEFORE the top-of-iteration-3 re-resolve runs (the exact eviction
    technique ``test_context_auto_1827_s4b.py`` / ``test_1909_external_tool_
    result_narrowing.py`` use for the turn-boundary self-clear witness — here
    happening WITHIN one turn's multi-round loop instead of across a turn
    boundary). Round 3's ``remember_shared`` dispatch must STILL be denied —
    the latch holds even though a live history re-scan at that instant would
    see no taint (the marker is gone).

    ★ Strip-falsify: this test is the one the monotonic latch exists for.
    Removing the latch (i.e. reverting the per-iteration narrowing decision
    to a pure live-scan of ``self._intra_turn_contextual_for_turn_fn()``
    with no ``self._untrusted_latched`` / ``self._untrusted_latched_
    permission`` carry-over) makes this test flip RED: round 2's live scan
    would see the (now-evicted) history as untainted, ``remember_shared``
    would succeed, and capability would have RECOVERED mid-turn after
    compaction — the taint-laundering hole this design closes. Verified by
    hand during development (temporarily reverting the latch branches to a
    bare re-resolve): RED as predicted, confirming the latch is load-bearing
    and not incidental.
    """
    monkeypatch.chdir(tmp_path)
    session = _make(SafetyConfig(loop=LoopConfig(intra_turn_untrusted_narrowing=True)))

    def _simulate_mid_turn_compaction() -> None:
        session.history = [
            m for m in session.history if not (m.meta or {}).get("external_source")
        ]

    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm(
            [
                _tool_call_result([{"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"}]),
                _tool_call_result([{"name": "list_agents", "args": {}, "id": "tc_trusted"}]),
                _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_denied"}]),
                _text_result("done"),
            ],
            # Fires exactly when round index 1 (the harmless list_agents
            # round) is SERVED — i.e. AFTER round 0's tool-result tainted
            # history AND AFTER the top-of-iteration-1 re-resolve already
            # latched (that re-resolve runs before this LLM call), but
            # BEFORE the top-of-iteration-2 re-resolve that precedes round
            # 2's (remember_shared) dispatch decision.
            on_round={1: _simulate_mid_turn_compaction},
        ),
    )
    await session._handle_user_message("look something up then remember it", chain_id="c1")

    assert not any((m.meta or {}).get("external_source") for m in session.history), (
        "the compaction simulation must have actually evicted the tainted entry"
    )
    (denied_msg,) = [m for m in session.history if m.tool_call_id == "tc_denied"]
    assert "tool_excluded" in str(denied_msg.content)
    assert "remember_shared" in str(denied_msg.content)
