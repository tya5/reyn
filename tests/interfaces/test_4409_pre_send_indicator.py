"""#4409 — a visible indicator distinguishing "not yet sent" from "sent,
awaiting response", closing the disappearing-message window the owner
reported (verbatim): "実際 msg enter -> sent 表示の間でメッセージ消える
こと多いので" (a message often seems to vanish between hitting enter and
appearing as "sent").

**Root cause, measured in this repo** (not guessed): ``on_composer_submitted``
already cleared the composer BEFORE awaiting
``ClientTransport.submit_user_text`` — a real network round trip for a
remote (AG-UI) client — and the sent-queue region only materializes a row
once the server's ``user_submitted`` broadcast arrives, on an INDEPENDENT
channel from that awaited call's own return
(``client_transport.py``'s own ``submit_user_text`` docstring, F2). Between
the composer clearing and that broadcast landing, the message was visible
in NEITHER place — exactly the owner's report, and exactly the window a
diagnostic (owner's ①: distinguish "stuck before being sent" from "sent,
awaiting response") needs to persist through to be useful at all.

**The fix reuses the existing #3300 sent-queue region** (owner instruction:
measure existing mechanisms before inventing a new state machine) — a
FOURTH, LOCAL entry into it (``SentQueue``'s own module docstring), shown
synchronously with the composer clearing, keyed by a client-generated id
and rendered with a distinct glyph (:data:`~reyn.interfaces.inline.textual_chat.sent_queue._SENDING_GLYPH`)
until the ``user_submitted`` echo carrying this submission's own
``client_ref`` promotes it (#5833; ``TextualChatApp._handle_user_
submitted_event``) — see that method's own docstring for why this no
longer waits on the server's POST-response ack at all.

Owner requirement ① ("入力欄から消えるのと同時に" — the exact same moment):
verified directly below by observing the composer and the sent-queue in
the SAME frame, with ``submit_user_text`` held open by a controllable gate
so the test proves the row is visible while the server has not yet
responded at all — not merely "eventually".

Owner requirement ② (agent-attach switches the displayed queue) is
verified separately — ``test_3310_n2_reset_hydrate.py`` already covers the
``session_attached`` reset barrier the ``/attach`` slash command
(``registry.attach`` → ``_announce_session_attached``) routes through; this
file does not duplicate that coverage.

Real ``TextualChatApp`` + a real ``ClientTransportStub`` subclass throughout
(no ``unittest.mock``) — mirrors ``test_3300_p2b_sentqueue_render.py``'s own
``QueueTransport``, extended with a controllable ``submit_user_text`` gate
this file's races need. Public reads only: ``SentQueue.rendered_texts()``
(the glyph is part of the rendered row, so no private-state peek is needed
to see which sub-state a row is in), ``item_count()``, ``Composer.text``.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event

_TEXT = "hello"


class _GatedTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` whose ``submit_user_text``
    does not return until the test releases :attr:`ack_gate` — the seam
    every race in this file needs: it lets a test observe app state WHILE
    the server round trip is still in flight, not just after it settles.

    ``submitted_texts`` records every call, for tests that assert the
    ordinary submission still reaches the transport unchanged."""

    def __init__(self, *, msg_id: str = "m1", fail: bool = False) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()
        self.ack_gate: "asyncio.Event" = asyncio.Event()
        #: Set the instant ``submit_user_text`` starts waiting on
        #: ``ack_gate`` — the seam a test awaits (mirrors
        #: ``test_3310_n2_reset_hydrate.py``'s own ``call_started`` pattern)
        #: to know the SYNCHRONOUS part of ``on_composer_submitted`` (the
        #: composer clear + local placeholder show, #4409) has already run,
        #: without polling or a fixed sleep.
        self.entered: "asyncio.Event" = asyncio.Event()
        self._msg_id = msg_id
        self._fail = fail
        self.submitted_texts: "list[str]" = []
        #: #5833: the ``client_ref`` this transport was actually called
        #: with — the SAME id ``on_composer_submitted`` minted as this
        #: submission's local placeholder key. A test reads this back to
        #: build a ``user_submitted`` fixture event that carries the exact
        #: fact ``_handle_user_submitted_event`` now promotes on, without
        #: reaching into the app's own private state to learn the id.
        self.last_client_ref: "str | None" = None

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:
        self.submitted_texts.append(text)
        self.last_client_ref = client_ref
        self.entered.set()
        await self.ack_gate.wait()
        if self._fail:
            raise RuntimeError("simulated submit failure")
        return self._msg_id

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


def _user_submitted(
    *, msg_id: str, chain_id: str, text: str, seq: int, client_ref: "str | None" = None,
) -> Event:
    meta = {"client_ref": client_ref} if client_ref is not None else {}
    return Event(
        type="user_submitted",
        data={"text": text, "chain_id": chain_id, "msg_id": msg_id, "seq": seq, "meta": meta},
    )


def _turn_started(*, chain_id: str, seq: int) -> Event:
    return Event(type="turn_started", data={"kind": "user", "chain_id": chain_id, "seq": seq})


def _is_confirmed(row: str) -> bool:
    """Whether a rendered sent-queue row is in the CONFIRMED (server-acked)
    sub-state — either its unselected glyph (``▷``) or its selected one
    (``▶``, ``sent_queue.py``'s own #3777 pairing; a lone row is selected
    by default, ``_selected_index = 0``) — as opposed to still SENDING
    (``◇``). Centralized so a test asserts the SUB-STATE, never a specific
    glyph that selection state could otherwise make it guess wrong about."""
    return ("▷" in row or "▶" in row) and "◇" not in row


def _flow_user_entries(app: TextualChatApp):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == "user"]


async def _type_and_submit_in_flight(pilot, transport: "_GatedTransport", text: str) -> "asyncio.Task":
    """Type ``text`` and press Enter, returning BEFORE the submission
    settles.

    ``on_composer_submitted`` is a single App-level async message handler,
    and Textual's message pump processes messages for one node
    sequentially — ``pilot.press("enter")`` awaits until that handler
    RETURNS, not merely until the key was queued. With
    :class:`_GatedTransport` holding ``submit_user_text`` open, a plain
    ``await pilot.press("enter")`` would therefore never return at all.
    Backgrounding the press as a task and awaiting ``transport.entered``
    (set the instant ``submit_user_text`` starts blocking — after the
    composer has already cleared and the local placeholder already shown,
    both synchronous, #4409) is how a test observes state WHILE the
    server round trip is still open, without polling or a fixed sleep."""
    for ch in text:
        await pilot.press(ch)
    task = asyncio.create_task(pilot.press("enter"))
    await transport.entered.wait()
    return task


async def _yield_until(condition) -> None:
    """Wait on a real condition WITHOUT going through ``pilot.pause()``.

    ``pilot.pause()`` waits for Textual's own message pump to go fully
    idle — which never happens while :func:`_type_and_submit_in_flight`'s
    backgrounded ``pilot.press("enter")`` task is still open on
    ``on_composer_submitted`` (a single App-level handler the pump
    processes to completion before it can call itself idle again).
    ``asyncio.sleep(0)`` is a plain scheduler yield, not a pump-quiescence
    wait, so it lets an INDEPENDENT task (``_pump_frames``, applying a
    ``push_event``ed frame) make progress without needing the deliberately
    -stuck one to finish first. CLAUDE.md's Ceiling rule still applies —
    no fixed iteration count is asserted on; CI's own ``--timeout`` is the
    real kill switch for a condition that never becomes true."""
    while not condition():
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# ① — no gap: the queue row appears in the SAME frame the composer clears.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_placeholder_appears_before_the_server_has_responded_at_all() -> None:
    """Tier 2b: with ``submit_user_text`` held open (never acked), the
    composer is already empty AND the sent-queue already shows the message
    — the owner's own report is exactly the window where this was NOT
    true. Non-vacuity: without the ``on_composer_submitted`` change this
    asserts against, the sent-queue is empty here (verified by temporarily
    reverting to the pre-#4409 two-line body — see the PR)."""
    transport = _GatedTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        task = await _type_and_submit_in_flight(pilot, transport, _TEXT)

        composer = app.query_one(Composer)
        assert composer.text == "", "composer must already be cleared"
        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_items(), (
            "the queue must already show the message — the server has not "
            "even responded yet (ack_gate is still closed)"
        )
        (row,) = sent_queue.rendered_texts()
        assert _TEXT in row
        assert "◇" in row, "a not-yet-confirmed row must render the SENDING glyph"
        assert not _is_confirmed(row), "must not claim server-confirmed before any ack"

        transport.ack_gate.set()
        await task


@pytest.mark.asyncio
async def test_placeholder_reaches_the_transport_with_the_typed_text() -> None:
    """Tier 2b: non-vacuity for the above — the local placeholder does not
    replace the real submission, it merely renders before it settles."""
    transport = _GatedTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        task = await _type_and_submit_in_flight(pilot, transport, _TEXT)
        assert transport.submitted_texts == [_TEXT]

        transport.ack_gate.set()
        await task


# ---------------------------------------------------------------------------
# ② — reconciliation: ack arrives, then (later) the broadcast too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_promotes_the_placeholder_in_place_no_duplicate() -> None:
    """Tier 2b: #5833 — the ``user_submitted`` ECHO (carrying THIS
    submission's own ``meta.client_ref``, ``transport.last_client_ref``)
    promotes the SAME row from SENDING to CONFIRMED (glyph flips ``◇`` →
    ``▷``) the moment it arrives — never a second row, never a
    remove-then-readd (item_count stays 1 throughout) — regardless of
    whether ``submit_user_text``'s own POST response (``ack_gate``, still
    closed here) has come back at all. Pre-#5833 this promotion waited on
    the ack instead; #5833 moves it entirely onto the echo (see
    ``TextualChatApp._handle_user_submitted_event``'s own docstring for
    why: the two travel on independent channels and either can arrive
    first, so waiting on one of them by design leaves the other's
    ordering unclosed)."""
    transport = _GatedTransport(msg_id="m1")
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        task = await _type_and_submit_in_flight(pilot, transport, _TEXT)
        sent_queue = app.query_one(SentQueue)
        assert sent_queue.item_count() == 1

        await transport.push_event(
            _user_submitted(
                msg_id="m1", chain_id="c1", text=_TEXT, seq=1,
                client_ref=transport.last_client_ref,
            )
        )
        # #4409 test-only note: NOT ``pilot.pause()`` — see ``_yield_until``'s
        # own docstring for why that would deadlock here (the backgrounded
        # ``on_composer_submitted`` task is still open on ``ack_gate``).
        await _yield_until(
            lambda: bool(sent_queue.rendered_texts())
            and _is_confirmed(sent_queue.rendered_texts()[0])
        )
        assert sent_queue.item_count() == 1, "the echo must promote in place, not add a row"
        (row,) = sent_queue.rendered_texts()
        assert _TEXT in row

        transport.ack_gate.set()
        await task


@pytest.mark.asyncio
async def test_late_broadcast_does_not_duplicate_the_row() -> None:
    """Tier 2b: a SECOND ``user_submitted`` delta for the SAME ``msg_id``
    (a replay/duplicate — the seq-gate's own job to reject, or, failing
    that, the ``has_row(msg_id)`` guard :meth:`_handle_user_submitted_
    event` still carries for a non-``client_ref`` delta) must be a no-op
    on the widget once the first one already promoted the row — never a
    second remove+readd."""
    transport = _GatedTransport(msg_id="m1")
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        task = await _type_and_submit_in_flight(pilot, transport, _TEXT)
        sent_queue = app.query_one(SentQueue)

        await transport.push_event(
            _user_submitted(
                msg_id="m1", chain_id="c1", text=_TEXT, seq=1,
                client_ref=transport.last_client_ref,
            )
        )
        await _yield_until(
            lambda: bool(sent_queue.rendered_texts())
            and _is_confirmed(sent_queue.rendered_texts()[0])
        )
        assert sent_queue.item_count() == 1

        # A duplicate of the SAME seq — the gate ``apply_user_submitted``
        # itself already applied must reject as stale.
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text=_TEXT, seq=1)
        )
        await asyncio.sleep(0)

        assert sent_queue.item_count() == 1
        (row,) = sent_queue.rendered_texts()
        assert _TEXT in row and _is_confirmed(row)

        transport.ack_gate.set()
        await task


@pytest.mark.asyncio
async def test_echo_before_ack_promotes_immediately_ack_is_then_a_no_op() -> None:
    """Tier 2b: #5833 — the OLD race this replaces (pre-#5833, verified by
    reverting to that shape): the ``user_submitted`` broadcast materializing
    the authoritative row BEFORE ``submit_user_text`` itself returns used
    to leave two rows (real + local placeholder) coexisting until the ack
    finally arrived and ``_reconcile_local_send`` dropped the redundant one.

    #5833 closes this structurally, not narrower: the echo carries THIS
    client's own ``client_ref``, so it recognises and rekeys its OWN
    placeholder as a FACT, immediately — there is never a second row to
    begin with, and the ack (once unblocked below) has nothing left to
    reconcile at all."""
    transport = _GatedTransport(msg_id="m1")
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        task = await _type_and_submit_in_flight(pilot, transport, _TEXT)
        sent_queue = app.query_one(SentQueue)
        assert sent_queue.item_count() == 1  # the local placeholder only

        await transport.push_event(
            _user_submitted(
                msg_id="m1", chain_id="c1", text=_TEXT, seq=1,
                client_ref=transport.last_client_ref,
            )
        )
        # #4409 test-only note: NOT ``pilot.pause()`` — see ``_yield_until``'s
        # own docstring for why that would deadlock here (the backgrounded
        # ``on_composer_submitted`` task is still open).
        await _yield_until(
            lambda: bool(sent_queue.rendered_texts())
            and _is_confirmed(sent_queue.rendered_texts()[0])
        )
        assert sent_queue.item_count() == 1, (
            "#5833: never a second row, even momentarily — the echo rekeys "
            "the SAME placeholder by client_ref, it does not add a new one"
        )
        (row,) = sent_queue.rendered_texts()
        assert _TEXT in row

        transport.ack_gate.set()
        await task
        await pilot.pause()

        assert sent_queue.item_count() == 1, "the now-irrelevant ack must not disturb the row"
        (row,) = sent_queue.rendered_texts()
        assert _TEXT in row and _is_confirmed(row)


@pytest.mark.asyncio
async def test_dispatch_before_ack_drops_the_placeholder_without_requeuing_it() -> None:
    """Tier 2b: the sharpest race — ``user_submitted`` AND ``turn_started``
    (the item is dispatched and promoted to a flow entry) both arrive
    before this client's own ack.

    #5833: the OLD mechanism this raced against (``_dispatched_before_ack``)
    is deleted; the structural fix removes the need for it. Ordering
    guarantees the server always emits ``user_submitted`` before it
    dispatches the SAME item — so the echo (carrying ``client_ref``)
    always rekeys the placeholder onto ``msg_id`` BEFORE ``turn_started``
    can fire for it, and by the time that dispatch removes the sent-queue
    row, it is already keyed by the server's own id. Nothing is left for
    a late ack to wrongly requeue."""
    transport = _GatedTransport(msg_id="m1")
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        task = await _type_and_submit_in_flight(pilot, transport, _TEXT)

        await transport.push_event(
            _user_submitted(
                msg_id="m1", chain_id="c1", text=_TEXT, seq=1,
                client_ref=transport.last_client_ref,
            )
        )
        await transport.push_event(_turn_started(chain_id="c1", seq=2))
        # #4409 test-only note: NOT ``pilot.pause()`` — see ``_yield_until``'s
        # own docstring for why that would deadlock here.
        await _yield_until(lambda: bool(_flow_user_entries(app)))
        (entry,) = _flow_user_entries(app)
        assert entry.item.text == _TEXT

        transport.ack_gate.set()
        await task
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert not sent_queue.has_items(), (
            "the already-dispatched item must not be requeued by a late ack"
        )
        assert len(_flow_user_entries(app)) == 1, "the dispatch's own promotion is untouched"


# ---------------------------------------------------------------------------
# ③ — submit failure: the placeholder must not linger claiming "sending".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_failure_removes_the_placeholder() -> None:
    """Tier 2b: a submission that ultimately fails must not leave a
    permanent "sending" row behind — the operator would read that as
    still in flight forever, the opposite of what a diagnostic exists
    for."""
    transport = _GatedTransport(fail=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        task = await _type_and_submit_in_flight(pilot, transport, _TEXT)
        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_items()

        transport.ack_gate.set()
        await task
        await pilot.pause()

        assert not sent_queue.has_items(), "a failed submit must not leave a stale placeholder"
