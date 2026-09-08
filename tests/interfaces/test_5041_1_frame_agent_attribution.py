"""Tier 2: #5041 ① — the live per-frame path (``DisplayFrame``/``EventFrame``)
now carries the origin agent's name.

Architect's own reading (issuecomment-5442805752, resolved by reading, not
observation — the 55-comment thread's "does repl_outbox mix N agents'
output" question was never one that needed a 2-agent-concurrent repro to
answer): ``registry.repl_outbox`` is a SINGLE process-wide queue every
attached agent's own forwarder writes onto, and neither ``OutboxMessage``
nor the frame types wrapping it carried any "whose" axis at all —
``BacklogBatch`` already does (``agent: str``, #5139, the reconnect/switch
snapshot path); only the LIVE per-frame path didn't. If a future redesign
ever let two agents be "attached" concurrently, a consumer draining the
converged stream would have no way to tell their frames apart — not
unlikely, structurally impossible.

Fix (``src/reyn/interfaces/transport/frames.py`` + ``in_process.py`` +
``registry.py``): ``DisplayFrame``/``EventFrame`` gain an optional
``agent: str | None = None`` field (mirrors ``BacklogBatch.agent``,
defaults to ``None`` so every OTHER existing construction site across the
AG-UI wire paths and test doubles is unaffected). ``InProcessTransport``
populates it two ways, NEITHER of which reads live "who's attached now"
state at the moment a frame is produced (architect's own TESTS-READ(B)
BLOCK finding, issuecomment-5443216808, on an EARLIER version of this fix
that read ``registry.attached_name`` in the audit-event path at call
time — an execution-order assumption a background-dispatched callback can
break, attaching a CONFIDENTLY WRONG name instead of the pre-fix "no
attribution at all", worse as an attribution failure mode):

- the ``repl_outbox``-draining path tracks a running ``current_agent``,
  updated ONLY by the ``session_attached`` barrier frame itself flowing
  through the SAME queue (never by re-querying live registry state at
  drain time, which could race a switch that happens between an item's
  PUT — gated on ``is_attached`` at THAT moment, in the registry's own
  ``_forwarder`` — and this GET). The barrier's own documented FIFO
  property (registry.py's ``_announce_session_attached``: "before this
  frame = old session's frames, after = new session's frames") is what
  makes queue-position-based tracking correct.
- the audit-event path has no such queue to track position in, so
  ``AgentRegistry._wire_focus_listeners`` instead BINDS the agent name
  into the subscribed closure at SUBSCRIBE time (wrapping the raw
  listener before ``session.subscribe_audit_events`` ever sees it) —
  ``EventLog`` dispatches via its own background consumer (#4961 C), not
  synchronously with ``emit()``, so a late-running callback still
  carries the name of whichever session it was ACTUALLY subscribed to.
  This closes the race STRUCTURALLY (no live state is ever read at call
  time to race against), not merely narrows its window — per architect's
  own note, there is deliberately no test attempting to force that
  specific timing here: once binding removes the only state a race could
  act on, there is nothing left for such a test to observe failing.

Real ``AgentRegistry`` + 2 real sessions (``make_session``) + a real
``InProcessTransport`` — no mocks. Drives ``session._put_outbox`` directly
(the seam under test; the session's own full router-loop run is not what's
being verified here) and ``registry.attach()`` for the switch. No polling,
no self-authored timeout: ``transport.frames().__anext__()`` suspends on a
real ``asyncio.Queue.get()`` the background forwarder/pump tasks feed, so
a bare, unbounded ``await`` on it IS the wait — CI's own ``--timeout`` is
the kill switch (CLAUDE.md's Ceiling rule), not a number chosen here.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session


def _registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile):
        return make_session(agent_name=profile.name, agent_role=profile.role)

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("beta")
    return reg


def _cancel_all(reg: AgentRegistry) -> None:
    for task in reg.running_tasks():
        task.cancel()


@pytest.mark.asyncio
async def test_live_display_frame_carries_the_attached_agents_name(tmp_path) -> None:
    """Tier 2: a message from the attached agent's own outbox is forwarded
    onto ``repl_outbox`` by the REAL registry forwarder, and the
    ``DisplayFrame`` ``InProcessTransport`` produces from it carries that
    agent's name — closing the "cannot tell whose frame this is, even in
    principle" finding for the single-attached case visible today, and
    threading the type-level axis through for a future N-attached one."""
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        alpha = reg.attached_session()
        assert alpha is not None
        transport = InProcessTransport(reg, intervention_channel="test-5041")
        transport.start()
        try:
            await alpha._put_outbox(OutboxMessage(kind="agent", text="hello from alpha"))
            # attach() itself already queued a session_attached EventFrame
            # (the #3310 N1 barrier) before this test's own message — skip
            # past it, same shape as the switch test below.
            frame = None
            while frame is None:
                candidate = await transport.frames().__anext__()
                if isinstance(candidate, DisplayFrame):
                    frame = candidate
            assert frame.message.text == "hello from alpha"
            assert frame.agent == "alpha", (
                f"#5041 REGRESSION: a live DisplayFrame should carry the "
                f"attached agent's name — got {frame.agent!r}"
            )
        finally:
            transport.close()
    finally:
        _cancel_all(reg)


@pytest.mark.asyncio
async def test_attribution_follows_a_real_switch_via_the_barrier(tmp_path) -> None:
    """Tier 2: after a real ``/attach``-style switch (alpha -> beta), a
    message from beta's own outbox is attributed to "beta", not stuck on
    the PREVIOUS agent's name — proving the barrier-based tracking (not a
    stale seed) is what drives attribution across a switch, the exact
    shape the queue-position (not real-time-state) design exists for."""
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        transport = InProcessTransport(reg, intervention_channel="test-5041")
        transport.start()
        try:
            await reg.attach("beta")
            beta = reg.attached_session()
            assert beta is not None
            await beta._put_outbox(OutboxMessage(kind="agent", text="hello from beta"))

            seen_agents: "list[str | None]" = []
            display_frame = None
            while display_frame is None:
                frame = await transport.frames().__anext__()
                if isinstance(frame, EventFrame):
                    seen_agents.append(frame.agent)
                    continue
                display_frame = frame

            assert display_frame.message.text == "hello from beta"
            assert display_frame.agent == "beta", (
                f"#5041 REGRESSION: after a switch, the live DisplayFrame should "
                f"follow the barrier to the NEW agent's name — got "
                f"{display_frame.agent!r}"
            )
        finally:
            transport.close()
    finally:
        _cancel_all(reg)


@pytest.mark.asyncio
async def test_audit_event_frame_carries_the_currently_attached_agents_name(
    tmp_path,
) -> None:
    """Tier 2: the OTHER live frame source — a session's own audit-event
    subscription — also carries attribution now, read fresh from
    ``registry.attached_name`` at each event (correct because that
    subscriber is only ever wired to the CURRENTLY attached session,
    re-wired on every switch — no queue-convergence race to guard against
    on this path, unlike ``repl_outbox``)."""
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        alpha = reg.attached_session()
        assert alpha is not None
        transport = InProcessTransport(reg, intervention_channel="test-5041")
        transport.start()
        try:
            # #5557: this emit only DRIVES frame creation on the audit-event
            # path — the assert below reads `frame.agent` (a PRODUCTION-
            # computed attribution field, read fresh from
            # `registry.attached_name` at frame-build time), never this
            # emit's own type/chain_id. The event.type filter just finds
            # the right frame; "turn_started" could be any forwarded kind.
            alpha._audit_events.emit("turn_started", chain_id="c-5041")
            frame = None
            while frame is None:
                candidate = await transport.frames().__anext__()
                if isinstance(candidate, EventFrame) and candidate.event.type == "turn_started":
                    frame = candidate
            assert frame.agent == "alpha", (
                f"#5041 REGRESSION: a live EventFrame from the audit-event path "
                f"should carry the currently-attached agent's name — got "
                f"{frame.agent!r}"
            )
        finally:
            transport.close()
    finally:
        _cancel_all(reg)
