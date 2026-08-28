"""Tier 2: #187 B1 — the router-loop swallow handler surfaces the exception.

Before #187 B1, a mid-turn router-loop exception (e.g. the final call_llm
raising after retries — root cause: 200 + empty choices) was swallowed at
``_handle_user_message``'s ``except Exception`` into a classified outbox
summary and a graceful return, silently terminating the turn with no
diagnosable trace (req=resp+1, no logged response).

This test confirms the instrument: when ``_run_router_loop`` raises, the
handler emits a ``router_loop_terminated_by_exception`` P6 event carrying the
error type + repr, so any future swallowed loop error is primary-evidence
(deny-message principle: surface, don't silently swallow). The classified
outbox message still goes out unchanged — the instrument is additive.

The failure is injected with a real async stub (a Fake that raises) — no
MagicMock — and the assertion reads the public EventLog surface via a real
``add_subscriber`` (``collect_events``), not private state.

#5332: the two ``..._logs_...`` tests below witness the SAME catch-all's log
LINE, not the audit event — architect's TESTS-READ(B) finding on the first
#5332 fix: the outcome half of that log line is conditioned on
``self._ephemeral`` (interactive keeps running vs. an agent-step leaf
re-raising), and nothing made a REVERT to an unconditional claim go red.
Both legs are driven through the real ``_handle_inbox_text`` path (the
ephemeral leg via ``pytest.raises``, since it genuinely propagates
``AgentStepError``) and read via ``caplog`` — the log line's own text, not
private state.
"""
from __future__ import annotations

import logging

import pytest

from reyn.runtime.errors import AgentStepError
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


@pytest.mark.asyncio
async def test_swallowed_router_loop_exception_emits_p6_event():
    """Tier 2: a mid-turn router-loop exception emits router_loop_terminated_by_exception (#187 B1).

    Invariant: the swallow handler does not silently swallow — it emits a P6
    event with the error type + repr so the root error is recoverable from the
    event log even when the outbox only carries a classified summary.
    """
    s = make_session(agent_name="t")
    collected = collect_events(s._audit_events)

    async def _raise_mid_work(text: str, chain_id: str) -> None:
        # Stand-in for the real mid-work crash (final call_llm raising after
        # retries). A real async callable — a Fake, not a mock.
        raise RuntimeError("simulated final-call crash")

    s._run_router_loop = _raise_mid_work  # inject the failure at the loop seam

    # Must not propagate — the handler swallows-but-surfaces.
    await s._handle_inbox_text("hello", chain_id="c-test")
    await settle(s._audit_events)

    terminated = [
        e for e in collected
        if e.type == "router_loop_terminated_by_exception"
    ]
    assert terminated, (
        "swallow handler must emit router_loop_terminated_by_exception so the "
        "root error is primary-evidence, not silently swallowed"
    )
    ev = terminated[0]
    assert ev.data["chain_id"] == "c-test"
    assert ev.data["error_type"] == "RuntimeError"
    assert "simulated final-call crash" in ev.data["error"]


@pytest.mark.asyncio
async def test_interactive_leg_logs_what_it_actually_did_not_that_the_session_continues(
    caplog: pytest.LogCaptureFixture,
):
    """Tier 2: #5332 — the interactive leg's log line names only what this
    except clause itself does (queues an error reply, returns normally),
    never a claim about what happens after it returns. Strip-falsify: revert
    the outcome text to an unconditional "the session continues" and this
    still passes (wrong text, but still non-ephemeral) — the SIBLING test
    below is what catches an unconditional revert, since it would then
    ALSO see "the session continues" on the ephemeral leg, which never
    returns to observe any such thing."""
    # make_session's default (no post-construction _ephemeral mutation,
    # unlike the sibling test below) is the interactive leg — asserted
    # by which log text appears, not by reading the attribute directly.
    s = make_session(agent_name="t")

    async def _raise_mid_work(text: str, chain_id: str) -> None:
        raise RuntimeError("simulated final-call crash")

    s._run_router_loop = _raise_mid_work

    with caplog.at_level(logging.ERROR, logger="reyn.runtime.session"):
        await s._handle_inbox_text("hello", chain_id="c-interactive")

    messages = [r.message for r in caplog.records]
    assert any("queued an error reply and returning normally" in m for m in messages), (
        f"#5332 REGRESSION: interactive leg's log line should name what it "
        f"actually did, not assert a session-lifetime claim — got {messages!r}"
    )
    assert not any("the session continues" in m for m in messages), (
        "the interactive leg must not claim the session continues — this "
        "except clause never observes what happens after it returns "
        "(owner's own #5329 report: a process reaching shell with the root "
        "cause still open)"
    )


@pytest.mark.asyncio
async def test_ephemeral_leg_logs_re_raising_and_actually_raises(
    caplog: pytest.LogCaptureFixture,
):
    """Tier 2: #5332 — the ephemeral leg (an agent-step spawn's leaf
    session) both logs "re-raising as AgentStepError" AND actually raises
    it — the log line's claim and the control flow are the SAME
    ``self._ephemeral`` check, so this test would go red on either half
    drifting from the other (the log line reverted to unconditional, or the
    raise itself removed)."""
    s = make_session(agent_name="t")
    s.mark_ephemeral()  # the registry sets this post-construction on a real ephemeral spawn

    async def _raise_mid_work(text: str, chain_id: str) -> None:
        raise RuntimeError("simulated final-call crash")

    s._run_router_loop = _raise_mid_work

    with caplog.at_level(logging.ERROR, logger="reyn.runtime.session"):
        with pytest.raises(AgentStepError):
            await s._handle_inbox_text("hello", chain_id="c-ephemeral")

    messages = [r.message for r in caplog.records]
    assert any("re-raising as AgentStepError" in m for m in messages), (
        f"#5332 REGRESSION: ephemeral leg's log line should say it is "
        f"re-raising — got {messages!r}"
    )
    assert not any(
        "queued an error reply and returning normally" in m for m in messages
    ), (
        "the ephemeral leg must not claim it queued a reply and returned "
        "normally — it re-raises, it never reaches that branch"
    )
