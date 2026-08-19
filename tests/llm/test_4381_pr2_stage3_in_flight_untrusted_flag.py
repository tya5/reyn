"""Tier 2: #4381 PR-2 stage ③ — the in-flight untrusted-taint latch.

architect's corrected design (posted to #4381, after the docs-maintainer's own
question surfaced a flaw in the first draft): the resource this PR touches is
NOT "no witness exists" — ``meta`` already carries ``external_source`` at
``router_loop.py``'s tool-result production, a persisted PROJECTION of the
taint, not its origin. The real problem is TIMING: ``_ephemeral_contextual_
for_turn`` (the per-iteration re-narrow check, #1909/#3501) reads
``self.history``, which only reflects what has already been COMMITTED — and
a LATER PR (#4381 ①, not yet implemented) will defer that commit to after
the turn's model response, opening a same-turn window where a tool result's
taint is real but invisible to the history scan.

This file pins the fix in isolation, ahead of ① landing: a single in-flight
boolean latch, set at the SAME line ``router_loop.py`` stamps
``external_source`` onto the persisted meta (no second, independently-
maintained signal), read by ``_ephemeral_contextual_for_turn``'s gate as an
OR alongside the history scan. Since ① does not exist yet, the "history has
not landed" precondition is constructed directly (monkeypatching the
captured ``RouterHostAdapter._append_history_cb`` to silently drop ONLY the
tainted tool-result entry) — the SAME technique
``test_1909_intra_turn_opt_in_narrowing.py``'s own compaction-survival test
already uses to construct an analogous "the record isn't there" edge case,
not a departure from this suite's established idiom.

Real ``Session`` + real history + real tool registry + scripted
``call_llm_tools`` throughout — no ``_FakeMessage``/hand-rolled host
stand-in (#2957 PR-A precedent, same as this file's sibling).

#4886 (measured, kept — not removed): under CURRENT wiring, ① still does
not exist, so ``_simulate_deferred_commit`` below is the ONLY way this
file's own precondition ("history has not landed") can be constructed —
real code paths append synchronously today, and this scenario cannot
occur through them. Once ① lands and defers the commit for real, this
file's own scripted flow starts exercising a genuinely reachable state
instead of a monkeypatched stand-in for one — same test, same witness,
different status of the thing it's watching.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.untrusted_narrowing import narrowing_on

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


def _scripted_llm(rounds: list):
    state = {"n": 0}

    async def _call(**kwargs: Any) -> LLMToolCallResult:
        idx = state["n"]
        r = rounds[idx]
        state["n"] += 1
        return r
    return _call


def _make() -> Session:
    return make_session(agent_name="test_agent", safety=narrowing_on("iteration"))


def _simulate_deferred_commit(session: Session) -> None:
    """Wrap the ALREADY-CAPTURED ``RouterHostAdapter._append_history_cb`` so
    an ``external_source``-tagged tool-result entry is silently dropped
    instead of committed — simulating what a FUTURE deferred-commit PR (①)
    will produce: the entry's taint is real (the in-flight flag still fires,
    from its own separate call site) but not yet visible to a history scan.

    Must patch the CAPTURED callback object, not ``session._append_history``
    itself — ``RouterHostAdapter.__init__`` bound the method ONCE at Session
    construction, so patching the instance attribute after the fact would
    not be observed by the callback the adapter already holds.
    """
    real_cb = session.router_host._append_history_cb

    def _drop_untrusted_tool_result(msg: Any) -> None:
        if msg.role == "tool" and (msg.meta or {}).get("external_source"):
            return
        real_cb(msg)

    session.router_host._append_history_cb = _drop_untrusted_tool_result


@pytest.mark.asyncio
async def test_flag_denies_when_history_has_not_landed_yet(tmp_path, monkeypatch):
    """Tier 2: ★ positive control. With the tainted tool-result's history
    entry simulated as not-yet-committed, the SAME-turn next dispatch
    (``remember_shared``) is still denied — the in-flight flag alone, with
    zero history support, is sufficient."""
    monkeypatch.chdir(tmp_path)
    session = _make()
    _simulate_deferred_commit(session)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([{"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"}]),
            _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_denied"}]),
            _text_result("done"),
        ]),
    )

    await session._handle_inbox_text("look something up then remember it", chain_id="c1")

    assert not any((m.meta or {}).get("external_source") for m in session.history), (
        "the simulated deferred-commit must have actually kept the tainted "
        "entry out of history — otherwise this test proves nothing about "
        "the flag (it would pass even via the ordinary history scan)"
    )
    (denied_msg,) = [m for m in session.history if m.tool_call_id == "tc_denied"]
    assert "tool_excluded" in str(denied_msg.content)
    assert "remember_shared" in str(denied_msg.content)


@pytest.mark.asyncio
async def test_falsify_without_the_flag_the_same_window_is_open(tmp_path, monkeypatch):
    """Tier 2: ★ falsify — the EXACT same setup as the positive control
    above, but with the flag-setting call itself neutralized. The denial
    must NOT happen: this proves the positive control's denial genuinely
    depends on the flag, not on some other, accidental mechanism (a bare
    "it denied" assertion with no falsify twin would pass even if the flag
    did nothing at all)."""
    monkeypatch.chdir(tmp_path)
    session = _make()
    _simulate_deferred_commit(session)
    # Neutralize ONLY the flag-setting path — RouterHostAdapter.
    # mark_untrusted_in_flight forwards to this callback; a no-op leaves the
    # gate with nothing but the (deliberately blinded) history scan.
    session.router_host._mark_untrusted_in_flight_cb = None
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([{"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"}]),
            _tool_call_result([{"name": "remember_shared", "args": _REMEMBER_ARGS, "id": "tc_allowed"}]),
            _text_result("done"),
        ]),
    )

    await session._handle_inbox_text("look something up then remember it", chain_id="c1")

    assert not any((m.meta or {}).get("external_source") for m in session.history), (
        "sanity: the deferred-commit simulation must still be in effect"
    )
    (msg,) = [m for m in session.history if m.tool_call_id == "tc_allowed"]
    assert "tool_excluded" not in str(msg.content), (
        "with the flag neutralized and history blinded, the same-turn "
        "window is genuinely open — this is the exact failure mode #4381 "
        "PR-2 exists to close, reproduced deliberately here as the flag's "
        "own falsify twin"
    )


# ── order-falsify (architect's 3rd adversarial angle) — NOT WRITTEN ────────
#
# architect's design names a THIRD failure mode: at turn end, clearing the
# in-flight flag BEFORE the deferred commit lands opens a window where BOTH
# signals are false (flag already cleared, history not yet committed).
#
# This test cannot be meaningfully written against the CURRENT codebase.
# Today there is no "commit" step separate from immediate persistence to
# reorder against the flag's clear — router_loop.py's tool-result append is
# synchronous and unconditional (§① has not landed), so by the time ANY
# turn-boundary flag-clear could run, every one of that turn's history
# entries is already, unconditionally, on disk. There is no reachable code
# path today where "clear" and "commit" could race, so a test asserting one
# would either not compile against real functions or would vacuously pass
# with a hand-built stand-in — the exact "third-party promise" / "constructs
# its own configuration" shape CLAUDE.md's test-review discipline (Tier
# question ③) rules out.
#
# This is exactly the material #4381 ①'s own brief needs: whichever PR
# implements the deferred commit must land the "commit, THEN clear" runtime
# ordering, and MUST include this order-falsify test (swap the two steps,
# confirm a same-turn dispatch inside the window is wrongly allowed) as its
# own acceptance condition — it cannot be satisfied here, before ① exists.
