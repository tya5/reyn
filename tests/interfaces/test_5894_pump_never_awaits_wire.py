"""Tier 2b: #5894 ①-2 — the TUI's message pump never awaits the wire.

Owner-hit: Ctrl-C's ``action_cancel_turn`` awaited ``cancel_inflight`` on the
pump. Textual delivers messages to an App serially, so while the remote
server answered nothing the handler held the pump, and NO later key was
delivered — Ctrl-Q included, which touches no wire at all (Textual's own
``App.exit()``). The operator saw a live spinner and a dead keyboard.

Architect ruling ①-2: a handler draws "requested" at once and returns; the
round-trip runs on a worker and its outcome is drawn when it is a fact.
These tests drive REAL keypresses on a real ``TextualChatApp`` (the same
end-to-end idiom ``test_ctrl_c_interrupt_3498.py`` established — calling
the action directly would not show whether the NEXT key gets through) over
a real minimal ``ClientTransport`` whose ``cancel_inflight`` HOLDS: it
awaits an ``asyncio.Event`` the test controls, so "the server has not
answered" is a state, not a duration. Nothing sleeps; nothing is timed.

What each proves, and what its red looks like:

- Ctrl-C then Ctrl-Q exits while the cancel is still held. Strip: put the
  ``await self._transport.cancel_inflight()`` back inline in
  ``action_cancel_turn`` and the Ctrl-Q press is never delivered — the red
  is a HANG that CI's ``--timeout`` kills, not an assertion; that is the
  shape of the defect itself (a key that never arrives), and the docstring
  says so here because a hang wears no colour of its own.
- The "cancel requested…" row is on the pane BEFORE the wire answers.
- A cancel the transport could not deliver (the EMPTY summary the ABC
  reserves for it, #5894) is named on the pane — the silence this issue
  was reported as gets a row (its exact wording comes from the typed
  outcome when the transport records one, #5907 ②).
- Ctrl-Q needs no wire at all: not this file's subject (Textual's binding,
  a one-line ``shutdown``), stated so nobody looks for it here.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _HeldCancelTransport(ClientTransportStub):
    """A real minimal transport whose ``cancel_inflight`` holds until the
    test releases it — the server that has not answered, as a state."""

    def __init__(self) -> None:
        self.started = 0
        self.release = asyncio.Event()

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

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

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> str:
        self.started += 1
        await self.release.wait()
        return "cancelled"

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass

    async def deliver_pending_answer(self, text: str) -> bool:
        return False


class _UnacknowledgedCancelTransport(_HeldCancelTransport):
    """``cancel_inflight`` returns at once with the EMPTY summary — what
    ``AgUiTransport`` returns after a control timeout (#5894 ①-1)."""

    async def cancel_inflight(self) -> str:
        self.started += 1
        return ""


def _pane_texts(app: TextualChatApp) -> list[str]:
    return [entry.item.text for entry in app.conversation]


@pytest.mark.asyncio
async def test_ctrl_q_exits_while_a_ctrl_c_cancel_is_still_unanswered() -> None:
    """Tier 2b: the owner-hit, as a keypress sequence. The cancel is in
    flight and held; Ctrl-Q must still be delivered and exit the app."""
    transport = _HeldCancelTransport()
    app = TextualChatApp(transport=transport)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert transport.started == 1, "setup: ctrl+c did not reach cancel_inflight"
            assert not transport.release.is_set(), "setup: the cancel must still be held"
            assert app.is_running, "setup: ctrl+c must not exit by itself"

            await pilot.press("ctrl+q")
            await pilot.pause()
            assert not app.is_running, (
                "ctrl+q was not delivered while cancel_inflight was unanswered — "
                "the pump is awaiting the wire again (#5894)"
            )
    finally:
        transport.release.set()


@pytest.mark.asyncio
async def test_cancel_requested_is_drawn_before_the_wire_answers() -> None:
    """Tier 2b: the "requested" row is the handler's own, synchronous act;
    the wire's answer (still held here) is not what puts it there. Once
    released, an acknowledged cancel adds no "not responding" row."""
    transport = _HeldCancelTransport()
    app = TextualChatApp(transport=transport)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not transport.release.is_set(), "setup: the cancel must still be held"
            texts = _pane_texts(app)
            assert any("cancel requested" in t for t in texts), (
                f"no 'cancel requested' row while the wire is unanswered; pane: {texts!r}"
            )

            transport.release.set()
            await pilot.pause()
            await pilot.pause()
            texts = _pane_texts(app)
            assert not any("not responding" in t for t in texts), (
                f"an acknowledged cancel was drawn as a non-delivery; pane: {texts!r}"
            )
    finally:
        transport.release.set()


@pytest.mark.asyncio
async def test_an_undelivered_cancel_is_named_not_responding() -> None:
    """Tier 2b: the transport's EMPTY summary (a control timeout / send
    failure) reaches the operator as a row — and the app stays up. This
    stub records no typed outcome, so the row is the fallback wording."""
    transport = _UnacknowledgedCancelTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        await pilot.pause()
        texts = _pane_texts(app)
        # #5907 ②: this stub records no typed outcome, so the row carries the
        # honest fallback ("did not acknowledge") — the refused / not-delivered
        # wording is the typed transport's (test_5907_typed_control_outcome.py).
        assert any("did not acknowledge the cancel" in t for t in texts), (
            f"an undelivered cancel left only 'requested' on the pane: {texts!r}"
        )
        assert app.is_running, "a non-delivery tore the app down"
