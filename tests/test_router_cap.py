"""Tier 4 (scaffold candidate): per-turn skill_router invocation cap (S4 dogfood follow-up).

Background: Pre-OSS dogfood S4 Run 3 logged 16 router invocations / 245k
prompt tokens for a single user paste — runaway loop with no upper bound.
Session now enforces a configurable cap (default 3) on consecutive
`skill_router` invocations within one user turn (or one fresh
agent_request).

**Tier classification (R-D6 audit)**: these tests inject private state
(``session.router_invocations_this_turn = 3``,
``_router_last_reason = ...``) and monkeypatch a private method
(``_reset_router_turn_counter``). Per
``docs/deep-dives/contributing/testing.ja.md`` this is Tier 4 — the test couples
to internals rather than the public surface.

Migration path: rewrite as a Tier 3 LLM-replay test that drives the cap
via real router invocations until it fires naturally. Until then,
these tests are kept (the cap is a security-relevant boundary that
shouldn't be left untested) but tagged as scaffold candidates for
removal once the replay-based test lands.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.config import LoopConfig, SafetyConfig
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.errors import RouterCapExceeded
from reyn.runtime.session import Session


def _run(coro):
    return asyncio.run(coro)


def _make_session(
    tmp_path: Path,
    *,
    cap: int = 3,
) -> Session:
    """Construct a minimal Session rooted at `tmp_path`. Workspace and
    events dirs are created under `.reyn/` relative to the chdir caller.
    The caller is responsible for chdir-ing into `tmp_path`.
    """
    safety = SafetyConfig(loop=LoopConfig(max_router_calls_per_turn=cap))
    return Session(
        agent_name="test_agent",
        budget_tracker=BudgetTracker(CostConfig()),
        safety=safety,
    )


def _drain_outbox(session: Session) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


# ── test 1: cap is enforced — fourth attempt is blocked ───────────────────────


def test_router_retry_cap_enforced(tmp_path, monkeypatch):
    """Tier 2: cap counter accumulates within a turn; once the cap is hit,
    _check_and_increment_router_cap raises RouterCapExceeded *before* any
    LLM call. The user gets the structured fallback reply.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3)

    # Pre-spend the budget so the next check crosses the cap.
    session._reset_router_turn_counter()
    session.router_invocations_this_turn = 3
    session._router_last_reason = "previous_reason"

    # The next attempt should overflow immediately via the cap check.
    # FP-0005: _check_and_increment_router_cap is now async (consults
    # safety.on_limit on hit). Default mode = unattended preserves the
    # legacy raise-immediately behaviour.
    with pytest.raises(RouterCapExceeded) as excinfo:
        asyncio.run(session._check_and_increment_router_cap("would_be_4th_call"))

    exc = excinfo.value
    assert exc.count == 3
    assert exc.cap == 3
    assert exc.last_reason == "previous_reason"

    # Counter is unchanged (no spurious increment on rejection).
    assert session.router_invocations_this_turn == 3


def test_handle_user_message_emits_fallback_when_cap_exhausted(
    tmp_path, monkeypatch,
):
    """Tier 2: when `_handle_user_message` hits the cap, the user sees a structured
    error + a polite agent fallback on the outbox, the event is emitted,
    and history records the fallback."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3)

    # Pre-spend so the very first router call in this turn is rejected.
    # Note: `_handle_user_message` resets the counter at its top, then
    # calls _run_router_loop — so to simulate exhaustion we monkeypatch
    # the reset to no-op AND pre-set the counter.
    monkeypatch.setattr(
        Session, "_reset_router_turn_counter", lambda self: None,
    )
    session.router_invocations_this_turn = 3
    session._router_last_reason = "out_of_scope"

    captured_events: list[dict] = []
    original_emit = session._chat_events.emit

    def capture(name, **kw):
        captured_events.append({"name": name, **kw})
        return original_emit(name, **kw)

    session._chat_events.emit = capture  # type: ignore[assignment]

    _run(session._handle_user_message("hello", chain_id="chain-x"))

    msgs = _drain_outbox(session)
    kinds = [m.kind for m in msgs]
    # status (考え中...), error (exhausted budget), agent (fallback)
    assert "error" in kinds, f"missing error message; got {kinds}"
    assert "agent" in kinds, f"missing agent fallback; got {kinds}"

    err = next(m for m in msgs if m.kind == "error")
    assert "exhausted retry budget" in err.text.lower()
    assert "3/3" in err.text
    assert "out_of_scope" in err.text

    # Event was emitted.
    names = [e["name"] for e in captured_events]
    assert "router_retry_exhausted" in names

    # History contains the fallback agent message tagged with the reason.
    # Issue #383: role rename "agent" → "assistant".
    fallback_msgs = [
        m for m in session.history
        if m.role == "assistant" and m.meta.get("source") == "router_cap_exhausted"
    ]
    assert fallback_msgs, "expected at least one router_cap_exhausted fallback in history"


# ── test 2: success within cap ────────────────────────────────────────────────


def test_router_succeeds_within_cap(tmp_path, monkeypatch):
    """Tier 2: two ordinary turns of router (now via RouterLoop) succeed; the
    counter accumulates correctly per turn (resets between fresh user
    turns).

    PR35: `_handle_user_message` now invokes RouterLoop. We patch
    `RouterLoop.run` to count invocations and emit a stub reply via the
    host's put_outbox callback.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3)

    call_count = {"n": 0}

    async def fake_router_run(self, user_text, history):
        call_count["n"] += 1
        # Mirror what real RouterLoop does on a chitchat reply: put a text
        # outbox via the host callback.
        await self.host.put_outbox(
            kind="agent", text="stub-reply",
            meta={"chain_id": self.chain_id},
        )

    from reyn.runtime.router_loop import RouterLoop
    monkeypatch.setattr(RouterLoop, "run", fake_router_run)

    # First turn: one router call, counter at 1, no exception.
    _run(session._handle_user_message("first message", chain_id="c1"))
    assert call_count["n"] == 1
    assert session.router_invocations_this_turn == 1

    # Second turn: counter resets to 0 then increments to 1 — well under cap.
    _run(session._handle_user_message("second message", chain_id="c2"))
    assert call_count["n"] == 2
    assert session.router_invocations_this_turn == 1

    # Outbox includes the agent reply for both turns.
    msgs = _drain_outbox(session)
    agent_replies = [m for m in msgs if m.kind == "agent"]
    assert agent_replies, "expected at least one agent reply per turn"
    assert all(m.text == "stub-reply" for m in agent_replies)

    # No exhaustion event was emitted.
    assert not any(m.kind == "error" for m in msgs)


def test_cap_zero_disables_check(tmp_path, monkeypatch):
    """Tier 2: cap=0 disables the per-turn cap entirely (escape hatch). Many
    consecutive _check_and_increment_router_cap calls should all go through
    without raising and without touching the counter."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=0)

    # With cap=0, _check_and_increment_router_cap returns immediately.
    # FP-0005: now async; await each call.
    for _ in range(20):
        asyncio.run(session._check_and_increment_router_cap("noop"))  # must not raise

    # Counter does not increment when cap is disabled.
    assert session.router_invocations_this_turn == 0
