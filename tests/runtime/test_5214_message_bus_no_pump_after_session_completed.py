"""Tier 2: #5214 — a completed session must never be pumped again.

Real-machine observation (issue #5214 body): a session's own ``run()``
loop had already exited (``session_halted`` → ``chat_stopped`` →
``session_completed`` all emitted), yet turns kept running for 4h20m
with NO further audit-events — band: audit-events ("the audit-event
trace is no longer sufficient to reconstruct what happened").

Root cause (lead-coder, issue comments): ``message_bus.py``'s pump loop
only ever checked ``agent.inbox.empty()`` — never whether the session's
own lifecycle had already ended — so a NEW request routed to an
already-completed agent kept calling ``run_one_iteration()`` forever.

Decision (c) (lead-coder, ruled in the issue, not re-litigated here):
``MessageBus`` must refuse to pump a completed session at all, rather
than (a) delaying ``session_completed`` or (b) recording post-
completion events on a separate surface — "the fix closes the hole
that lets a session run past its own end record, it does not paper
over the record itself."

``Session.run_completed`` (the new public read-point #5214 adds — none
existed before, public or private) is driven for REAL here via
``session.shutdown()`` + a real ``session.run()`` to natural
completion — never hand-set as private state.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.message_bus import MessageBus
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.session import Session
from reyn.runtime.transport import McpRef
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> Session:
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


async def _run_to_real_completion(session: Session) -> None:
    """Drive ``session.run()`` to a REAL, natural exit via the public
    ``shutdown()`` sentinel (never hand-setting ``_run_completed``) —
    the same path a real shutdown takes, so ``run_completed`` becomes
    True for the same reason it would in production."""
    await session.shutdown()
    await session.run()


@pytest.mark.asyncio
async def test_run_completed_is_false_before_and_true_after_real_completion(
    tmp_path,
) -> None:
    """Tier 2: #5214 — sanity/control for the new read-point itself:
    False while running (never started), True only after a REAL
    run()-to-exit, driven via the public shutdown() sentinel."""
    session = _make_session(tmp_path)
    assert session.run_completed is False

    await _run_to_real_completion(session)

    assert session.run_completed is True


@pytest.mark.asyncio
async def test_message_bus_still_pumps_a_session_that_has_not_completed(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #5214 control arm (acceptance ②) — ordinary pre-completion
    behavior is UNCHANGED: a live session's inbox message is still
    processed via run_one_iteration()."""
    session = _make_session(tmp_path)

    async def _fake_handle_inbox_text(self, text, *, chain_id):
        await self._put_outbox(OutboxMessage(kind="agent", text=f"echo:{text}"))

    monkeypatch.setattr(Session, "_handle_inbox_text", _fake_handle_inbox_text)

    bus = MessageBus()
    replies = await bus.request(
        session, kind="user", payload={"text": "hello"},
        reply_to=McpRef(request_id="live-001"), timeout=5.0,
    )

    agent_texts = [r.text for r in replies if r.kind == "agent"]
    assert "echo:hello" in agent_texts
    assert session.run_completed is False


@pytest.mark.asyncio
async def test_message_bus_refuses_to_pump_a_completed_session(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #5214 acceptance ①④ — the headline defect. After the
    session's own run() has REALLY completed, a NEW MessageBus.request
    call (simulating a new inbound request routed to an already-dead
    agent — the real-machine shape) must NOT invoke run_one_iteration()
    again, even though the request puts a fresh message on the inbox.

    Witness: a call-COUNTING wrapper around the real
    Session.run_one_iteration — this observation point changes value
    (increments) exactly when the claim under test is false (pumping
    happened), and stays unchanged when the claim holds. Not "an
    audit-event was emitted" (a different, weaker claim this issue's
    own root cause shows can be true even while the underlying pumping
    keeps happening) — the assertion is on the ACTUAL pump call itself.
    """
    session = _make_session(tmp_path)

    async def _fake_handle_inbox_text(self, text, *, chain_id):
        await self._put_outbox(OutboxMessage(kind="agent", text=f"echo:{text}"))

    monkeypatch.setattr(Session, "_handle_inbox_text", _fake_handle_inbox_text)

    await _run_to_real_completion(session)
    assert session.run_completed is True, (
        "test setup sanity: the session must have really completed "
        "before this test's own subject (refusing to pump it) applies"
    )

    call_count = 0
    real_run_one_iteration = Session.run_one_iteration

    async def _counting_run_one_iteration(self):
        nonlocal call_count
        call_count += 1
        return await real_run_one_iteration(self)

    monkeypatch.setattr(Session, "run_one_iteration", _counting_run_one_iteration)

    bus = MessageBus()
    replies = await bus.request(
        session, kind="user", payload={"text": "too late"},
        reply_to=McpRef(request_id="late-001"), timeout=0.5,
    )

    assert call_count == 0, (
        "run_one_iteration() was called on a session whose own run() had "
        "already completed — MessageBus must refuse to pump it at all"
    )
    assert replies == []


@pytest.mark.asyncio
async def test_message_bus_discloses_the_stuck_message_not_a_silent_drop(
    tmp_path, caplog,
) -> None:
    """Tier 2: #5214 acceptance ③ — refusing to pump a completed session
    must not be SILENT: the message this call put on the inbox stays
    there, unconsumed, and that fact is logged (not just returned as an
    empty reply list indistinguishable from "nothing happened")."""
    session = _make_session(tmp_path)
    await _run_to_real_completion(session)

    bus = MessageBus()
    with caplog.at_level("WARNING", logger="reyn.runtime.message_bus"):
        await bus.request(
            session, kind="user", payload={"text": "stuck"},
            reply_to=McpRef(request_id="stuck-001"), timeout=0.5,
        )

    assert any(
        "already completed" in r.message and "will NOT be consumed" in r.message
        for r in caplog.records
    ), (
        "refusing to pump a completed session must be disclosed, not a "
        f"silent no-op — records: {[r.message for r in caplog.records]!r}"
    )
    # The message itself is not silently discarded — it is still sitting
    # in the inbox, exactly where the disclosure above says it is.
    assert session.inbox.qsize() == 1
