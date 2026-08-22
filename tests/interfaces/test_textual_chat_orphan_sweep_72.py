"""Force-settle orphaned RUNNING tools at the turn boundary (#72).

An ORPHAN is a tool whose completion frame never arrives (the report is lost,
or the turn ends without it) — without a fix, its ② live spinner
(``⠙ elapsed Ns``) spins FOREVER. The design (architect-settled, F5c) is a
TURN-BOUNDARY sweep, deliberately NOT a max-age timer: a time threshold cannot
distinguish an orphan (tool truly gone) from a slow-but-alive tool (a
legitimately long ``exec``), but the turn-completion signal is deterministic —
when the turn settles there can be no more completions for that turn's tools.

These gates:

- **non-vacuity** (Tier 2b): an orphaned RUNNING tool + a ``turn_settled`` event
  settles the entry (animation stopped, neutral body, state CANCELLED — not
  RUNNING). Proven non-vacuous by the paired
  ``test_without_the_sweep_the_orphan_would_stay_running`` witness, which
  removes the trigger (no turn-end event delivered) and shows the entry stays
  RUNNING/marked-live — the sweep call is what closes the gap.
- **no-false-kill** (Tier 2b): a tool that COMPLETED within the turn (already
  popped from ``_running_tools`` by the coalesce path) is untouched by the
  sweep — its coalesced ``⎿ result`` and SUCCESS state survive ``turn_settled``.
- **neutral-not-coral** (Tier 1 + Tier 2b): the settled-incomplete entry's state
  is NOT ``EntryState.ERROR`` (nor ``SUCCESS``) and its body is the dim
  ``⎿ (no result — turn ended)`` line, never a ``✗`` failure summary — the
  #3296 don't-fabricate-a-failure lesson applied to the orphan case.
- **deterministic** (Tier 2b): the sweep fires off the ``turn_settled`` /
  ``turn_completed`` / ``turn_cancelled`` EVENT frame, never a clock/sleep —
  pinned by driving a frozen, non-advancing clock through the whole test.

All use a real, mounted :class:`TextualChatApp`, real
:class:`~reyn.runtime.outbox.OutboxMessage` / :class:`~reyn.interfaces.transport.frames.EventFrame`,
and a real :class:`~reyn.schemas.models.Event` — no ``MagicMock``, per the
testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.presenter import (
    _RESULT_KIND_KEY,
    _RUNNING_SINCE_KEY,
    ReynPresenter,
)
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


def _started(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_started", text=tool, meta={"tool": tool, "op_id": op_id, "args": {}}
    )


def _completed(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_completed",
        text="",
        meta={"tool": tool, "op_id": op_id, "result": {"op": tool, "count": 3}},
    )


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue — display OR event frames — so a test can push a ``started`` tool,
    inspect its live RUNNING row, THEN push a turn-end event and inspect the
    sweep, with the stream staying open throughout."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    async def push_event(self, event_type: str) -> None:
        await self._queue.put(EventFrame(Event(type=event_type)))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _entries(app: TextualChatApp):
    return app.query_one(FlowView).entries


@pytest.mark.asyncio
async def test_orphaned_running_tool_settles_neutral_on_turn_settled() -> None:
    """Tier 2b: an orphaned RUNNING tool + ``turn_settled`` settles it — animation
    stopped, state CANCELLED (not RUNNING, not spinning), neutral body."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_started("op-orphan"))
        await pilot.pause()
        entry = _entries(app)[0]
        assert entry.state is EntryState.RUNNING
        assert (entry.item.meta or {}).get(_RUNNING_SINCE_KEY) is not None

        await transport.push_event("turn_settled")
        await pilot.pause()

        assert entry.state is EntryState.CANCELLED
        assert (entry.item.meta or {}).get(_RUNNING_SINCE_KEY) is None
        meta = entry.item.meta or {}
        assert meta.get(_RESULT_KIND_KEY) not in ("tool_call_completed", "tool_call_failed")
        pres = await ReynPresenter(clock=lambda: 100.0).present(entry, 80)
        body = pres.renderable
        from rich.console import Console

        console = Console(width=80)
        with console.capture() as cap:
            console.print(body)
        text = cap.get()
        assert "no result — turn ended" in text
        assert "✗" not in text


@pytest.mark.asyncio
async def test_without_the_sweep_the_orphan_would_stay_running() -> None:
    """Tier 2b: non-vacuity witness — with NO turn-end event ever delivered, the
    orphaned tool stays RUNNING and marked live — proving the sweep call (not
    some other mechanism) is what closes the gap in the paired test above."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_started("op-still-running"))
        await pilot.pause()
        await pilot.pause()
        entry = _entries(app)[0]
        assert entry.state is EntryState.RUNNING
        assert (entry.item.meta or {}).get(_RUNNING_SINCE_KEY) is not None


@pytest.mark.asyncio
async def test_completed_tool_is_untouched_by_the_sweep() -> None:
    """Tier 2b: no-false-kill — a tool that COMPLETED within the turn (already
    popped from ``_running_tools`` by the coalesce path) is not re-touched by
    the sweep; its coalesced result + SUCCESS state survive ``turn_settled``."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_started("op-done"))
        await pilot.pause()
        await transport.push_display(_completed("op-done"))
        await pilot.pause()
        entry = _entries(app)[0]
        assert entry.state is EntryState.SUCCESS
        result_kind_before = (entry.item.meta or {}).get(_RESULT_KIND_KEY)
        assert result_kind_before == "tool_call_completed"

        await transport.push_event("turn_settled")
        await pilot.pause()

        assert entry.state is EntryState.SUCCESS
        assert (entry.item.meta or {}).get(_RESULT_KIND_KEY) == "tool_call_completed"
        pres = await ReynPresenter(clock=lambda: 100.0).present(entry, 80)
        from rich.console import Console

        console = Console(width=80)
        with console.capture() as cap:
            console.print(pres.renderable)
        assert "⎿" in cap.get()


@pytest.mark.asyncio
async def test_turn_completed_and_turn_cancelled_also_sweep() -> None:
    """Tier 2b: the belt-and-suspenders turn-end events (``turn_completed`` /
    ``turn_cancelled``) trigger the same sweep as the primary ``turn_settled``."""
    for event_type in ("turn_completed", "turn_cancelled"):
        transport = QueueTransport()
        app = TextualChatApp(transport=transport, clock=lambda: 100.0)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await transport.push_display(_started(f"op-{event_type}"))
            await pilot.pause()
            entry = _entries(app)[0]
            assert entry.state is EntryState.RUNNING

            await transport.push_event(event_type)
            await pilot.pause()

            assert entry.state is EntryState.CANCELLED, event_type
