"""Tier 2b: #5907 ① — the TUI's pump never awaits a slash command's wire
round-trip, and the round-trips run one at a time, in typed order.

Owner-hit class (#5894): with the server not answering, a command that
touched the wire held the message pump for the whole wait, and every later
key — Ctrl-Q included — queued behind it. #5902 closed that for Enter and
Ctrl-C; the slash layer's own wire calls (27 of 32 commands, #5907 census)
still ran on the handler, bounded only by the control timeout T. The
architect's ruling ①: T is one round-trip's bound, not the operator's (N
presses → N×T), and the only exit lives in the pump that is stuck — so the
dispatcher hands its run unit to a worker, and the app keeps a SINGLE
in-flight FIFO so effects keep the order the serial pump used to give.

Real keypresses on a real ``TextualChatApp`` over a real minimal
``ClientTransport`` whose ``request_session_list`` HOLDS on an
``asyncio.Event`` the test controls — "the server has not answered" as a
state, never a duration. The plain CUI is untouched (no ``runner``).

What each proves, and its red:

- While ``/session list`` is held, Ctrl-Q still exits. Strip: run the
  dispatcher's unit inline (``runner=None`` in ``_submit``) — the Ctrl-Q
  press is never delivered; the red is a HANG CI's ``--timeout`` kills,
  disclosed here because a hang wears no colour.
- FIFO: ``/help`` typed behind the held ``/session list`` draws NOTHING
  until the list is released, then both draw, list first. Strip: run each
  unit on its own worker — ``/help``'s rows appear while the list is still
  held → red, deterministically (the first unit never returns until the
  test says so).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _HeldListTransport(ClientTransportStub):
    """A real minimal transport: ``request_session_list`` holds until
    released; everything it is told to display is recorded on the app's
    own frame path (so the pane shows it) — ``put_display`` re-enters the
    stream the app pumps, exactly as ``InProcessTransport`` does."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.list_calls = 0
        self._frames: "asyncio.Queue[DisplayFrame]" = asyncio.Queue()

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield await self._frames.get()

    def put_display(self, msg: "OutboxMessage") -> None:
        self._frames.put_nowait(DisplayFrame(msg))

    async def request_session_list(self) -> "list[dict]":
        self.list_calls += 1
        await self.release.wait()
        return [{"sid": "main", "attached": True}]

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self):
        return None

    async def cancel_inflight(self) -> str:
        return "cancelled"

    async def shutdown(self) -> None:
        pass


def _pane_texts(app: TextualChatApp) -> list[str]:
    return [entry.item.text for entry in app.conversation]


async def _type_command(pilot, app: TextualChatApp, text: str) -> None:
    app.query_one(Composer).focus()
    await pilot.pause()
    await pilot.press(*text)
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_ctrl_q_exits_while_session_list_is_held_on_the_wire() -> None:
    """Tier 2b: the owner-hit shape, for a slash command. The list's
    round-trip is in flight and held; Ctrl-Q must still be delivered."""
    transport = _HeldListTransport()
    app = TextualChatApp(transport=transport)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _type_command(pilot, app, "/session list")
            assert transport.list_calls == 1, "setup: /session list never reached the wire"
            assert not transport.release.is_set(), "setup: the list must still be held"
            assert app.is_running

            await pilot.press("ctrl+q")
            await pilot.pause()
            assert not app.is_running, (
                "ctrl+q was not delivered while /session list was unanswered — "
                "the dispatcher's unit is awaited on the pump again (#5907 ①)"
            )
    finally:
        transport.release.set()


@pytest.mark.asyncio
async def test_units_run_one_at_a_time_in_typed_order() -> None:
    """Tier 2b: single in-flight FIFO. ``/help`` typed behind a held
    ``/session list`` draws nothing until the list is released; then the
    list's result precedes ``/help``'s."""
    transport = _HeldListTransport()
    app = TextualChatApp(transport=transport)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _type_command(pilot, app, "/session list")
            await _type_command(pilot, app, "/help")
            await pilot.pause()
            assert transport.list_calls == 1, "setup: the list unit never started"
            held_texts = _pane_texts(app)
            assert not any("Slash commands:" in t for t in held_texts), (
                "/help ran while /session list was still in flight — units are "
                f"concurrent, not a FIFO; pane: {held_texts!r}"
            )

            transport.release.set()
            while not any("Slash commands:" in t for t in _pane_texts(app)):
                await pilot.pause()
            texts = _pane_texts(app)
            list_idx = next(i for i, t in enumerate(texts) if "main" in t and "session" in t.lower())
            help_idx = next(i for i, t in enumerate(texts) if "Slash commands:" in t)
            assert list_idx < help_idx, f"results out of typed order: {texts!r}"
    finally:
        transport.release.set()
