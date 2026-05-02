"""Tests for the per-turn skill_router invocation cap (S4 dogfood follow-up).

Background: Pre-OSS dogfood S4 Run 3 logged 16 router invocations / 245k
prompt tokens for a single user paste — runaway loop with no upper bound.
ChatSession now enforces a configurable cap (default 3) on consecutive
`skill_router` invocations within one user turn (or one fresh
agent_request). Reset happens at the top of each fresh turn; in-chain
re-invocations (`agent_response`, `_resolve_pending_chain`) accumulate
against the same budget.

These tests bypass real LLM calls by patching `ChatSession._run_stdlib_skill`
so each invocation increments a counter and returns a canned router result.
The cap-check itself lives entirely in OS-level chat code — no skill
content is involved — so a unit test against `_handle_user_message` is the
right altitude.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.chat.session import ChatSession, RouterCapExceeded
from reyn.kernel.runtime import RunResult


def _run(coro):
    return asyncio.run(coro)


def _make_session(
    tmp_path: Path,
    *,
    cap: int = 3,
) -> ChatSession:
    """Construct a minimal ChatSession rooted at `tmp_path`. Workspace and
    events dirs are created under `.reyn/` relative to the chdir caller.
    The caller is responsible for chdir-ing into `tmp_path`.
    """
    cost = CostConfig(router_invocations_per_turn=cap)
    bt = BudgetTracker(cost)
    return ChatSession(
        agent_name="test_agent",
        budget_tracker=bt,
    )


def _drain_outbox(session: ChatSession) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def _stub_router_result() -> RunResult:
    """Canned router output that emits a reply but spawns no skills.
    `_handle_user_message` will dispatch this with no further router
    re-invocation (no messages_to_agents, no pending chain). Used to
    drive the counter from outside without triggering side-effects.
    """
    return RunResult(
        data={
            "control": {
                "type": "finish",
                "decision": "finish",
                "next_phase": None,
                "reason": {"summary": "stub_reason"},
            },
            "reply_text": "stub-reply",
            "skills_to_run": [],
            "messages_to_agents": [],
        },
        status="finished",
    )


# ── test 1: cap is enforced — fourth attempt is blocked ───────────────────────


def test_router_retry_cap_enforced(tmp_path, monkeypatch):
    """The cap counter accumulates within a turn; once the cap is hit,
    further `_invoke_router` raises RouterCapExceeded *before* the LLM
    is called. The user gets the structured fallback reply.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3)

    # Pre-spend the budget so the next _invoke_router crosses the cap.
    session._reset_router_turn_counter()
    session._router_invocations_this_turn = 3
    session._router_last_reason = "previous_reason"

    call_count = {"n": 0}

    async def fake_run_stdlib_skill(
        self, skill_name, input_artifact, *, state_subdir,
        mcp_servers=None, forward_events=False,
    ):
        call_count["n"] += 1
        return _stub_router_result()

    monkeypatch.setattr(
        ChatSession, "_run_stdlib_skill", fake_run_stdlib_skill,
    )

    # The next attempt should overflow.
    with pytest.raises(RouterCapExceeded) as excinfo:
        _run(session._invoke_router("would_be_4th_call"))

    exc = excinfo.value
    assert exc.count == 3
    assert exc.cap == 3
    assert exc.last_reason == "previous_reason"

    # The LLM must not have been called for the 4th attempt.
    assert call_count["n"] == 0, (
        "skill_router was invoked despite the cap being exhausted"
    )

    # Counter is unchanged (no spurious increment on rejection).
    assert session._router_invocations_this_turn == 3


def test_handle_user_message_emits_fallback_when_cap_exhausted(
    tmp_path, monkeypatch,
):
    """When `_handle_user_message` hits the cap, the user sees a structured
    error + a polite agent fallback on the outbox, the event is emitted,
    and history records the fallback."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3)

    # Pre-spend so the very first router call in this turn is rejected.
    # Note: `_handle_user_message` resets the counter at its top, then
    # calls `_invoke_router` — so to simulate exhaustion we monkeypatch
    # the reset to no-op AND pre-set the counter.
    monkeypatch.setattr(
        ChatSession, "_reset_router_turn_counter", lambda self: None,
    )
    session._router_invocations_this_turn = 3
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
    fallback_msgs = [
        m for m in session.history
        if m.role == "agent" and m.meta.get("source") == "router_cap_exhausted"
    ]
    assert len(fallback_msgs) == 1


# ── test 2: success within cap ────────────────────────────────────────────────


def test_router_succeeds_within_cap(tmp_path, monkeypatch):
    """Two ordinary turns of `_invoke_router` succeed; the counter
    accumulates correctly per turn (resets between fresh user turns).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3)

    call_count = {"n": 0}

    async def fake_run_stdlib_skill(
        self, skill_name, input_artifact, *, state_subdir,
        mcp_servers=None, forward_events=False,
    ):
        call_count["n"] += 1
        return _stub_router_result()

    monkeypatch.setattr(
        ChatSession, "_run_stdlib_skill", fake_run_stdlib_skill,
    )

    # First turn: one router call, counter at 1, no exception.
    _run(session._handle_user_message("first message", chain_id="c1"))
    assert call_count["n"] == 1
    assert session._router_invocations_this_turn == 1

    # Second turn: counter resets to 0 then increments to 1 — well under cap.
    _run(session._handle_user_message("second message", chain_id="c2"))
    assert call_count["n"] == 2
    assert session._router_invocations_this_turn == 1

    # Outbox includes the agent reply for both turns.
    msgs = _drain_outbox(session)
    agent_replies = [m for m in msgs if m.kind == "agent"]
    assert len(agent_replies) == 2
    assert all(m.text == "stub-reply" for m in agent_replies)

    # No exhaustion event was emitted.
    # (We don't capture events here — absence of error messages on the
    # outbox is sufficient.)
    assert not any(m.kind == "error" for m in msgs)


def test_cap_zero_disables_check(tmp_path, monkeypatch):
    """cap=0 disables the per-turn cap entirely (escape hatch). Many
    consecutive invocations should all go through."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=0)

    call_count = {"n": 0}

    async def fake_run_stdlib_skill(
        self, skill_name, input_artifact, *, state_subdir,
        mcp_servers=None, forward_events=False,
    ):
        call_count["n"] += 1
        return _stub_router_result()

    monkeypatch.setattr(
        ChatSession, "_run_stdlib_skill", fake_run_stdlib_skill,
    )

    for _ in range(20):
        _run(session._invoke_router("noop"))

    assert call_count["n"] == 20
    # Counter does not increment when cap is disabled.
    assert session._router_invocations_this_turn == 0
