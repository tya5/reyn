"""#3300 Y-client — the client half of cancel-by-id.

Y-server (PR #3306) landed ``Session.cancel_queued(msg_id)`` (snapshot-prune +
WAL tombstone + skip-at-consume) and the ``inbox_cancel`` audit-event delta;
``ClientTransport.cancel_queued(msg_id)`` exists on both transports and
``submit_user_text`` now returns the assigned ``msg_id`` (PR #3309). This file
covers the REMAINING client half, scoped to
``src/reyn/interfaces/inline/textual_chat/``:

1. **Event-driven row removal** — the sent-queue row is removed by the
   ``inbox_cancel`` delta, NEVER by ``cancel_queued``'s own return value (an
   automated strip proves this: the call returning ``True`` does not, by
   itself, remove the row — only the delta that follows does).
2. **Canceller-local composer restore** — on a successful cancel, the
   cancelled text is prepended at the HEAD of the composer (newline boundary
   when the composer already holds a draft), cursor at the END of the
   restored text; a client that did NOT issue the cancel applies only the
   removal (no restore).
3. **★Untrusted-text render surface (composer)** — fidelity + neutralize,
   independently strip-verified, no cross-masking with the sent-queue row's
   own (separate) neutralize site.
4. **Keybinding affordance** — ``↑`` from the composer (non-empty
   sent-queue) focuses the queue region; ``↑``/``↓`` move the highlighted
   row; ``Enter`` fires the cancel; ``Escape``/``Tab`` return to the
   composer.
5. **``DOMNode.is_empty`` non-collision** (co-vet finding on #3314) —
   ``SentQueue`` exposes its own empty/non-empty state as ``has_items()``,
   never a same-named ``is_empty`` method: Textual's base
   ``DOMNode.is_empty`` is a PROPERTY the ``:empty`` CSS pseudo-class hook
   (``textual/widget.py``) reads as ``widget.is_empty`` — a same-named
   method override would make that read return an always-truthy bound
   method, permanently flipping ``:empty`` ON for this widget.
6. **Help-pane discoverability** (co-vet finding on #3314) — the new
   composer/sent-queue keys are sourced into the Help pane from
   ``chrome.py``'s ``COMPOSER_KEYS``/``SENTQUEUE_KEYS`` constants, the SAME
   single source of truth ``MENUBAR_KEYS`` already uses (no second,
   undiscoverable hardcoding).

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` throughout — no
``unittest.mock``. Renders/state observed via the PUBLIC surface
(``SentQueue.rendered_texts()``/``selected_msg_id()``/``has_items()``,
``Composer.text``/``cursor_location``, ``FlowView.entries``), never private
state.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import (
    COMPOSER_KEYS,
    SENTQUEUE_KEYS,
    Composer,
    help_pane_lines,
)
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event

_RAW_ESC_OSC = "\x1b[31mRED\x1b]0;pwn\x07"


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from
    a queue (mirrors ``test_3300_p2b_sentqueue_render.py``'s helper), whose
    ``cancel_queued`` is CONTROLLABLE (a real attribute, not a mock) so a test
    can prove the row/composer state is driven by the ``inbox_cancel`` delta
    that follows, not by this call's return value."""

    def __init__(self, *, cancel_result: bool = True) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()
        self.cancel_result = cancel_result
        self.cancel_calls: "list[str]" = []

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> str:  # pragma: no cover
        return ""

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

    async def cancel_queued(self, msg_id: str) -> bool:
        self.cancel_calls.append(msg_id)
        return self.cancel_result

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _user_submitted(*, msg_id: str, chain_id: str, text: str, seq: int) -> Event:
    return Event(
        type="user_submitted",
        data={"text": text, "chain_id": chain_id, "msg_id": msg_id, "seq": seq, "meta": {}},
    )


def _inbox_cancel(*, msg_id: str, seq: int) -> Event:
    return Event(type="inbox_cancel", data={"msg_id": msg_id, "seq": seq})


def _flow_user_entries(app: TextualChatApp):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == "user"]


# ---------------------------------------------------------------------------
# 1. Event-driven removal (never return-value-driven)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_removes_row_only_on_inbox_cancel_delta_not_on_call_return() -> None:
    """Tier 2b: the row survives the ``cancel_queued`` call returning
    ``True`` by itself — it is removed only once the matching
    ``inbox_cancel`` delta actually arrives on the SAME frame path."""
    transport = QueueTransport(cancel_result=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="cancel me", seq=1)
        )
        await pilot.pause()
        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_items()

        await app.on_sent_queue_cancelled(SentQueue.Cancelled("m1"))
        assert transport.cancel_calls == ["m1"]
        assert sent_queue.has_items(), (
            "the row must NOT be removed by the cancel_queued call's return "
            "value alone — only the inbox_cancel delta removes it"
        )

        await transport.push_event(_inbox_cancel(msg_id="m1", seq=2))
        await pilot.pause()
        assert not sent_queue.has_items(), "the inbox_cancel delta must remove the row"


@pytest.mark.asyncio
async def test_strip_inbox_cancel_handler_leaves_row_stale(monkeypatch) -> None:
    """Tier 2b: non-vacuity — neutering the app's ``inbox_cancel`` delta
    handler (the wiring the positive test above depends on) leaves the row
    present despite the delta arriving, proving the positive assertion is
    not vacuous."""
    transport = QueueTransport(cancel_result=True)
    app = TextualChatApp(transport=transport)
    monkeypatch.setattr(
        TextualChatApp, "_handle_inbox_cancel_event", lambda self, event: None,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="cancel me", seq=1)
        )
        await pilot.pause()
        await transport.push_event(_inbox_cancel(msg_id="m1", seq=2))
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_items(), "stripped handler should leave the row stale"


@pytest.mark.asyncio
async def test_inbox_cancel_for_unrelated_msg_id_is_a_noop() -> None:
    """Tier 2b: non-vacuity — an ``inbox_cancel`` for a DIFFERENT msg_id does
    not touch an unrelated queued item."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="still queued", seq=1)
        )
        await pilot.pause()
        await transport.push_event(_inbox_cancel(msg_id="some-other-id", seq=2))
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert "still queued" in sent_queue.rendered_texts()[0]


# ---------------------------------------------------------------------------
# 2. Canceller-local composer restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canceller_restores_text_prepended_at_head_with_newline_boundary() -> None:
    """Tier 2b: the CANCELLER's composer gets the cancelled text prepended
    at the HEAD, even with an existing non-empty draft, separated by a
    newline boundary — the draft is not clobbered — and the cursor lands at
    the END of the restored text."""
    transport = QueueTransport(cancel_result=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="original message", seq=1)
        )
        await pilot.pause()

        composer = app.query_one(Composer)
        composer.text = "my next draft"

        await app.on_sent_queue_cancelled(SentQueue.Cancelled("m1"))
        await transport.push_event(_inbox_cancel(msg_id="m1", seq=2))
        await pilot.pause()

        assert composer.text == "original message\nmy next draft"
        assert composer.cursor_location == (0, len("original message"))


@pytest.mark.asyncio
async def test_canceller_restore_into_empty_composer_has_no_stray_newline() -> None:
    """Tier 2b: restoring into an EMPTY composer does not add a spurious
    trailing newline boundary (there is nothing to separate from)."""
    transport = QueueTransport(cancel_result=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="original message", seq=1)
        )
        await pilot.pause()

        await app.on_sent_queue_cancelled(SentQueue.Cancelled("m1"))
        await transport.push_event(_inbox_cancel(msg_id="m1", seq=2))
        await pilot.pause()

        composer = app.query_one(Composer)
        assert composer.text == "original message"
        assert composer.cursor_location == (0, len("original message"))


@pytest.mark.asyncio
async def test_non_canceller_client_applies_only_removal_no_restore() -> None:
    """Tier 2b: a client that did NOT issue the cancel (no matching entry in
    ``_pending_own_cancels``) sees the SAME ``inbox_cancel`` delta and
    removes the row, but the composer is untouched — canceller-local restore
    (owner-ratified contract, issue #3300 §6a)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="a peer's message", seq=1)
        )
        await pilot.pause()

        composer = app.query_one(Composer)
        composer.text = "my own untouched draft"

        # No on_sent_queue_cancelled call here — this client never asked to
        # cancel "m1"; the delta arrives from a PEER's cancel action instead.
        await transport.push_event(_inbox_cancel(msg_id="m1", seq=2))
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert not sent_queue.has_items(), "the peer's cancel delta still removes the row here"
        assert composer.text == "my own untouched draft", (
            "a non-canceller client must not restore any text into its composer"
        )


# ---------------------------------------------------------------------------
# 3. ★Untrusted-text render surface — composer restore (#3302-class gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restored_composer_text_neutralizes_raw_esc_osc_injection() -> None:
    """Tier 2b: ★security gate — the composer is a NEW render surface for a
    cancelled item's text (independent of the sent-queue row's own
    neutralize site). A raw ESC/OSC payload must not survive into
    ``composer.text``, while the harmless literal remainder still renders
    (fidelity)."""
    transport = QueueTransport(cancel_result=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text=_RAW_ESC_OSC, seq=1)
        )
        await pilot.pause()

        await app.on_sent_queue_cancelled(SentQueue.Cancelled("m1"))
        await transport.push_event(_inbox_cancel(msg_id="m1", seq=2))
        await pilot.pause()

        composer = app.query_one(Composer)
        assert "\x1b" not in composer.text, "raw ESC byte leaked into the restored composer text"
        assert "RED" in composer.text


@pytest.mark.asyncio
async def test_strip_composer_neutralize_leaks_raw_esc_independent_of_sent_queue_site() -> None:
    """Tier 2b: non-vacuity, no cross-masking — neutering ONLY the composer
    restore's own neutralize call (``_restore_cancelled_text``, patched here
    to skip it) leaks the raw ESC byte into ``composer.text`` even though
    the SENT-QUEUE row's OWN (separate) neutralize site is untouched and
    still clean — proving the two sites are independent witnesses, neither
    masking the other."""
    from reyn.interfaces.inline.textual_chat import app as app_module

    def _restore_without_neutralize(self, text: str) -> None:
        composer = self.query_one(Composer)
        existing = composer.text
        composer.text = f"{text}\n{existing}" if existing else text
        lines = text.split("\n")
        composer.move_cursor((len(lines) - 1, len(lines[-1])))

    monkeypatch_target = app_module.TextualChatApp
    original = monkeypatch_target._restore_cancelled_text
    monkeypatch_target._restore_cancelled_text = _restore_without_neutralize
    try:
        transport = QueueTransport(cancel_result=True)
        app = TextualChatApp(transport=transport)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await transport.push_event(
                _user_submitted(msg_id="m1", chain_id="c1", text=_RAW_ESC_OSC, seq=1)
            )
            await pilot.pause()

            sent_queue = app.query_one(SentQueue)
            # The SENT-QUEUE row's own (separate) neutralize site is intact —
            # its rendered content stays clean regardless of the composer strip.
            (row,) = sent_queue.rendered_texts()
            assert "\x1b" not in row

            await app.on_sent_queue_cancelled(SentQueue.Cancelled("m1"))
            await transport.push_event(_inbox_cancel(msg_id="m1", seq=2))
            await pilot.pause()

            composer = app.query_one(Composer)
            assert "\x1b" in composer.text, (
                "stripping the composer's OWN neutralize call should leak the "
                "raw ESC byte here, independent of the sent-queue row's site"
            )
    finally:
        monkeypatch_target._restore_cancelled_text = original


# ---------------------------------------------------------------------------
# 4. Keybinding affordance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_up_from_composer_focuses_nonempty_sent_queue() -> None:
    """Tier 2b: ``↑`` on the composer's first line focuses the sent-queue
    region when it holds at least one item — the mirror image of the
    composer's existing ``↓``-to-menubar rule."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="queued", seq=1)
        )
        await pilot.pause()

        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_focus


@pytest.mark.asyncio
async def test_up_from_composer_with_empty_sent_queue_moves_cursor_not_focus() -> None:
    """Tier 2b: non-vacuity — with an EMPTY sent-queue, ``↑`` never steals
    focus (falls through to ordinary cursor movement)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert not sent_queue.has_focus
        assert composer.has_focus


@pytest.mark.asyncio
async def test_enter_on_highlighted_row_cancels_it_and_escape_returns_focus() -> None:
    """Tier 2b: within the focused sent-queue, ↑/↓ move the highlighted row
    (by msg_id, never a guessed position), Enter cancels the highlighted
    row, and Escape returns focus to the composer."""
    transport = QueueTransport(cancel_result=True)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_user_submitted(msg_id="m1", chain_id="c1", text="alpha", seq=1))
        await transport.push_event(_user_submitted(msg_id="m2", chain_id="c2", text="beta", seq=2))
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        sent_queue.focus()
        await pilot.pause()
        assert sent_queue.selected_msg_id() == "m1"

        await pilot.press("down")
        await pilot.pause()
        assert sent_queue.selected_msg_id() == "m2"

        await pilot.press("enter")
        await pilot.pause()
        assert transport.cancel_calls == ["m2"]

        await transport.push_event(_inbox_cancel(msg_id="m2", seq=3))
        await pilot.pause()
        assert sent_queue.rendered_texts() and "alpha" in sent_queue.rendered_texts()[0]
        assert not any("beta" in t for t in sent_queue.rendered_texts())

        # "m1" is still queued, so the region is still focusable/visible;
        # Escape must send focus back to the composer regardless.
        await pilot.press("escape")
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.has_focus


# ---------------------------------------------------------------------------
# 5. ``DOMNode.is_empty`` non-collision witness (co-vet finding on #3314)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sent_queue_does_not_shadow_domnode_is_empty_property() -> None:
    """Tier 2b: ★``SentQueue`` must NOT override Textual's base
    ``DOMNode.is_empty`` PROPERTY with a same-named method — the ``:empty``
    CSS pseudo-class hook (``textual/widget.py``'s
    ``"empty": lambda widget: widget.is_empty``) reads it as a property; a
    method override would make that read return an always-truthy bound
    method, permanently flipping ``:empty`` ON for this widget regardless of
    its actual content. Reads the SAME attribute Textual's pseudo-class
    evaluator reads (``widget.is_empty``, no call parens) and asserts it is
    a real ``bool``, not a bound method — the exact collision shape the
    co-vet finding described."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        sent_queue = app.query_one(SentQueue)
        assert isinstance(sent_queue.is_empty, bool), (
            "SentQueue.is_empty must stay Textual's base DOMNode property "
            "(a bool) — a same-named method override would make this a "
            "bound method (always truthy), permanently flipping the "
            "':empty' CSS pseudo-class ON"
        )

        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="queued", seq=1)
        )
        await pilot.pause()
        assert sent_queue.is_empty is False, (
            "the base property should correctly read 'has children' once a "
            "row is mounted"
        )


# ---------------------------------------------------------------------------
# 6. Help-pane discoverability (co-vet finding on #3314)
# ---------------------------------------------------------------------------


def test_help_pane_lists_composer_up_arrow_and_sentqueue_keys() -> None:
    """Tier 2b: the new composer ``↑`` binding and the sent-queue's own keys
    (↑/↓ move, Enter cancel, Esc back to composer — #3365 dropped Tab from
    this row, see ``SENTQUEUE_KEYS``) must be DISCOVERABLE through the Help
    pane — ``chrome.py``'s own module docstring states the
    Help pane sources composer/menu keys from ``COMPOSER_KEYS``/
    ``MENUBAR_KEYS`` "rather than re-hardcoding a second copy"; the new
    sent-queue keys must follow the SAME single-source-of-truth convention
    (``SENTQUEUE_KEYS``), not go undocumented. Pure-function pane formatter —
    no widget mount needed."""
    lines = help_pane_lines()
    # #3327: ↑ now targets the pending intervention panel FIRST, the
    # sent-queue as its fallback — the description text was updated to say
    # so; "sent queue" itself stays a substring of the new text either way.
    assert any("sent queue" in line for line in lines), (
        "composer's ↑ -> sent-queue fallback binding is missing from "
        "COMPOSER_KEYS / the Help pane"
    )
    assert any("cancel" in line.lower() for line in lines), (
        "the sent-queue's cancel binding is missing from SENTQUEUE_KEYS / "
        "the Help pane"
    )


def test_composer_keys_and_sentqueue_keys_are_the_single_source_help_reads() -> None:
    """Tier 2b: non-vacuity — the Help pane's composer/sent-queue rows are
    EXACTLY ``COMPOSER_KEYS``/``SENTQUEUE_KEYS`` (not a hand-duplicated
    copy): every entry in each constant appears verbatim in the rendered
    Help lines."""
    lines = help_pane_lines()
    for key, desc in (*COMPOSER_KEYS, *SENTQUEUE_KEYS):
        assert any(key in line and desc in line for line in lines), (
            f"({key!r}, {desc!r}) from COMPOSER_KEYS/SENTQUEUE_KEYS is not "
            "rendered verbatim in the Help pane"
        )
