"""Tier 2: #5830 -- web/connect's status bar refreshes on the attach-time
STATE_SNAPSHOT itself, not on whatever frame happens to arrive next
(owner-hit).

Root cause (architect's own measurement, issue #5830): applying a decoded
``StateUpdate`` onto ``RemoteStatusView`` (``AgUiTransport._consume_block``)
never produced anything for :meth:`TextualChatApp._pump_frames` to iterate
-- the new values landed on the read-model instantly, but the ONE place
that calls ``_refresh_live_chrome`` (this pump's own per-frame trailer) had
nothing to fire on for a snapshot/delta-only SSE block, so the redraw
waited for the next turn's own frame.

Fix (architect FINAL ruling, same shape #5139 already established for
``MESSAGES_SNAPSHOT``/``BacklogBatch``): a decoded STATE_SNAPSHOT/STATE_DELTA
now ALSO yields one in-stream ``StatusApplied`` item -- see that class's own
docstring.

Real ``AgUiEmitter``/``AgUiTransport``/``RemoteReadModel``/``TextualChatApp``
throughout -- no mocks (mirrors test_agui_remote_inline_p3.py's own
established pattern in this directory).
"""
from __future__ import annotations

import pytest

from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import StatusApplied


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


async def _empty_frames():
    return
    yield  # pragma: no cover -- makes this an async generator


async def _noop_send(_payload):
    return None


async def _build_snapshot_only_transport(state: dict) -> AgUiTransport:
    """A real ``AgUiTransport`` fed a real reconnect-protocol SSE stream
    (``AgUiEmitter``'s own connect-time preamble: an EMPTY ``MESSAGES_
    SNAPSHOT`` + one ``STATE_SNAPSHOT`` carrying *state*) with NO further
    frames -- the exact "display frame を1つも流さない" shape the issue's
    own acceptance criterion names."""
    emitter = AgUiEmitter(_empty_frames(), lambda: dict(state))
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_SNAPSHOT" in sse
    assert "MESSAGES_SNAPSHOT" in sse
    return AgUiTransport(_sse_lines(sse), _noop_send)


# --- client.py level: the transport itself yields StatusApplied ---------------


@pytest.mark.asyncio
async def test_state_snapshot_alone_yields_a_status_applied_item() -> None:
    """Tier 2: draining a real AgUiTransport fed ONLY the connect-time
    reconnect protocol (empty backlog + one STATE_SNAPSHOT, zero display
    frames) yields a StatusApplied item -- not silently absorbed as a side
    channel. Falsifiable: reverting client.py's own `out.append(
    StatusApplied())` makes this collect zero StatusApplied items."""
    state = {"attached_name": "researcher", "model": "opus"}
    transport = await _build_snapshot_only_transport(state)

    kinds = []
    async for frame in transport.frames():
        kinds.append(type(frame).__name__)

    assert "StatusApplied" in kinds, (
        f"no StatusApplied item in the decoded stream: {kinds}"
    )
    assert "BacklogBatch" in kinds  # the empty reconnect backlog, #5139


@pytest.mark.asyncio
async def test_status_applied_carries_no_data_values_already_landed() -> None:
    """Tier 2: by the time StatusApplied is even constructed, RemoteReadModel
    already reflects the new snapshot -- the item's only job is to exist as
    a stream marker, never a second data path."""
    state = {"attached_name": "researcher", "model": "opus"}
    transport = await _build_snapshot_only_transport(state)
    read_model = RemoteReadModel(transport)

    saw_status_applied = False
    async for frame in transport.frames():
        if isinstance(frame, StatusApplied):
            saw_status_applied = True
            # Values are ALREADY on the read model at the instant this
            # item is seen -- no additional apply step is needed.
            snap = read_model.snapshot()
            assert snap["attached_name"] == "researcher"
    assert saw_status_applied


# --- app.py level: the real status bar refreshes with ZERO display frames -----


@pytest.mark.asyncio
async def test_status_line_shows_the_new_agent_immediately_on_attach() -> None:
    """Tier 2: the owner's own reported symptom, reproduced and closed --
    a real TextualChatApp wired to a real AgUiTransport shows the NEW
    agent's name in its status line the instant the attach-time STATE_
    SNAPSHOT decodes, with NO turn started and NOT ONE display frame ever
    pushed onto the wire. Before #5830's fix, this stayed on the
    read-model's construction-time default (empty/placeholder) until some
    later frame happened to arrive -- there is deliberately no later frame
    here, so a regression that removes StatusApplied (or reverts _pump_
    frames' own fall-through) leaves the OLD value on screen and this goes
    red."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    state = {"attached_name": "researcher", "model": "opus", "cost_agent": 0.0}
    transport = await _build_snapshot_only_transport(state)
    read_model = RemoteReadModel(transport)
    app = TextualChatApp(transport=transport, read_model=read_model)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        text = str(app.query_one(StatusLine).render())
        assert "researcher" in text, (
            f"status line did not pick up the attach-time snapshot with "
            f"zero display frames: {text}"
        )


# --- the "もう1段悪い箇所": all_sessions_status listener fires on snapshot too --


@pytest.mark.asyncio
async def test_all_sessions_status_listener_fires_on_snapshot_not_only_delta() -> None:
    """Tier 2: architect design ② -- a snapshot is a delta from the empty
    set. Before #5830, `_dispatch_status_listeners` only ever fired for a
    decoded STATE_DELTA (`AgUiTransport._consume_block`'s own pre-fix
    branch), so an attach's own `all_sessions_status` row -- arriving on
    the connect-time SNAPSHOT, never a later delta -- reached no listener
    until an UNRELATED future delta happened to touch that key. This is
    the "もう1段悪い箇所" the issue names as permanently stale without its
    own fix (a frame-arrival fix alone cannot close it)."""
    state = {
        "attached_name": "researcher",
        "all_sessions_status": [
            {"agent": "researcher", "sid": "main", "turn_active": True, "iv_waiting": False},
        ],
    }
    transport = await _build_snapshot_only_transport(state)

    seen: list[tuple] = []
    transport.add_status_listener(
        lambda agent, sid, turn_active, iv_waiting, seq: seen.append(
            (agent, sid, turn_active, iv_waiting)
        )
    )

    async for _frame in transport.frames():
        pass

    assert ("researcher", "main", True, False) in seen, (
        f"all_sessions_status row from the SNAPSHOT never reached the "
        f"listener: {seen}"
    )
