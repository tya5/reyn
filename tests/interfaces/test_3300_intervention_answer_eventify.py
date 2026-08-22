"""#3300 (event-ify the intervention-answer echo) — the last piece of the
"echo does not belong on the outbox" arc.

``InterventionHandler.deliver_answer_to`` (``runtime/services/
intervention_handler.py``) used to broadcast a resolved answer's DISPLAY text
as a ``kind="user"`` outbox frame — the SAME category error #3301/P1(C)
retired for the ordinary-submit path (an INPUT written into the display/
OUTPUT channel). This migrates the answer-echo to an
``intervention_answer_submitted`` audit-event, following the ``user_submitted``
precedent exactly: RAW text on the wire, each consuming surface neutralizes
at its OWN render boundary.

``tests/runtime/test_user_echo_broadcast.py`` (Part B) covers the producer side
(``InterventionHandler`` itself: the event's shape, attribution, fence-
orthogonality, the absence of an outbox frame). This file covers the TWO
SURFACE-SIDE consumers the producer-side tests cannot reach:

1. ``TextualChatApp`` (the default interactive TTY surface,
   ``_handle_intervention_answer_event`` / ``_pump_frames``) — the answer
   MUST still reach the flow view (the ADR-0039 multi-client property the
   retired outbox broadcast provided must not regress), and must neutralize
   raw ESC/OSC at ITS OWN render boundary (the surface never trusted the
   producer to have already stripped it — #3300's RAW-on-the-wire model).
   ★ #3540 NARROWED WHAT THESE COVER: the handler now FOLDS an answer into
   the ``kind="intervention"`` entry its ``intervention_id`` identifies, and
   appends a row of its own only when no such entry exists. Every TUI test
   here pushes an answer event into an app that never saw the announce, so
   they are the FALLBACK leg's witnesses — which is exactly the leg whose
   ADR-0039 reachability + own-boundary neutralization they were written for.
   The fold leg (and the live-vs-restore entry-sequence gate) lives in
   ``tests/interfaces/test_3540_intervention_answer_fold.py``.
2. ``ConsoleChatRenderer`` / ``InlineChatRenderer`` (the plain/--cui and
   plain-render-mode-fallback surfaces, ``on_audit_event`` ->
   ``intervention_answer_display_message``) — same neutralize-at-boundary
   witness, unit-level (no Textual app needed for these two).

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` (mirrors
``test_3300_p2b_sentqueue_render.py``'s ``QueueTransport`` harness) for (1);
real renderer instances + a real ``Event`` for (2). No ``unittest.mock``.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.repl.renderer import (
    ConsoleChatRenderer,
    InlineChatRenderer,
    intervention_answer_display_message,
)
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event

_RAW_ESC_OSC = "\x1b[31mRED\x1b]0;pwn\x07"


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time
    (identical harness to ``test_3300_p2b_sentqueue_render.py``'s
    ``QueueTransport`` — kept local rather than shared to avoid a
    cross-test-file import coupling for a 25-line fake)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

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


def _answer_event(*, text: str, meta: "dict | None" = None) -> Event:
    return Event(
        type="intervention_answer_submitted",
        data={"intervention_id": "iv-1", "text": text, "meta": meta or {}},
    )


def _flow_user_entries(app: TextualChatApp):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == "user"]


# ---------------------------------------------------------------------------
# 1. TextualChatApp — the answer echo reaches the default TTY surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intervention_answer_event_appends_flow_entry_directly() -> None:
    """Tier 2b: an "intervention_answer_submitted" event with NO announced
    question entry on this surface renders straight to the flow (no
    sent-queue staging — an intervention answer was never a queued inbox
    item, unlike "user_submitted"). #3540: this is the fallback leg, the one
    a client attached after the announce takes.

    Strip-falsify: removing the ``elif etype ==
    "intervention_answer_submitted"`` branch in ``_pump_frames`` (or the
    ``_handle_intervention_answer_event`` method itself) makes this event an
    EventFrame with no handler — per the frame vocabulary's own contract,
    silently consumed-but-dropped — so this assertion goes RED: the answer
    echo would vanish for the default interactive TTY surface, an ADR-0039
    regression (every attached surface must see the answer)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_answer_event(text="Tokyo"))
        await pilot.pause()

        (entry,) = _flow_user_entries(app)
        assert entry.item.text == "Tokyo"


@pytest.mark.asyncio
async def test_intervention_answer_event_neutralizes_raw_esc_osc_injection() -> None:
    """Tier 2: the payload carries RAW text (#3300 design pin — the producer
    no longer pre-neutralizes); THIS surface neutralizes at its own render
    boundary (``_neutralized_label``, the same seam ``turn_started``
    promotion uses) — a control/ESC byte in a free-text answer OR an
    LLM-derived choice label must not reach this TTY.

    Strip-falsify (recorded per repo discipline): temporarily removing the
    ``_neutralized_label(...)`` call in
    ``TextualChatApp._handle_intervention_answer_event`` reproduces the ESC
    byte in ``entry.item.text`` directly against this assertion."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_answer_event(text=_RAW_ESC_OSC))
        await pilot.pause()

        (entry,) = _flow_user_entries(app)
        assert "\x1b" not in entry.item.text
        assert "RED" in entry.item.text


@pytest.mark.asyncio
async def test_intervention_answer_event_carries_attribution_meta() -> None:
    """Tier 2: attribution (a peer's ``auth_user_id``) survives onto the
    rendered flow entry's meta — the ADR-0039 property #3305 already had to
    repair once for the sent-queue path; this is the SAME property for the
    answer-echo path."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _answer_event(text="Osaka", meta={"actor": "bob", "auth_user_id": "bob"})
        )
        await pilot.pause()

        (entry,) = _flow_user_entries(app)
        assert entry.item.meta.get("actor") == "bob"


@pytest.mark.asyncio
async def test_intervention_answer_event_ingest_failure_does_not_kill_pump(monkeypatch) -> None:
    """Tier 2: a single malformed answer-echo event must not kill the frame
    pump (the same "a single frame's failure must not kill the pump" contract
    every other handler in ``_pump_frames`` gets, per its docstring) —
    subsequent frames still ingest."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)

    def _boom(self, event) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        TextualChatApp, "_handle_intervention_answer_event", _boom,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_answer_event(text="this one explodes"))
        await pilot.pause()
        # The pump must have survived — a second, unrelated frame still ingests.
        await transport.push_event(Event(type="__noop__", data={}))
        await pilot.pause()
        assert app.is_running


# ---------------------------------------------------------------------------
# 2. ConsoleChatRenderer / InlineChatRenderer — the plain-surface consumers
# ---------------------------------------------------------------------------


def test_intervention_answer_display_message_neutralizes_raw_text() -> None:
    """Tier 1: the shared helper both plain renderers call neutralizes ESC/
    control bytes at the render boundary, mirroring
    ``user_submitted_display_message`` exactly."""
    ev = _answer_event(text=_RAW_ESC_OSC, meta={"actor": "alice"})

    msg = intervention_answer_display_message(ev)

    assert msg.kind == "user"
    assert "\x1b" not in msg.text
    assert "RED" in msg.text
    assert msg.meta.get("actor") == "alice"


def test_console_chat_renderer_renders_intervention_answer_event() -> None:
    """Tier 2: ``ConsoleChatRenderer.on_audit_event`` renders an
    "intervention_answer_submitted" event via ``message()`` — the plain
    ``--cui`` / non-TTY surface's consumer of the migrated echo."""
    rendered: list[OutboxMessage] = []
    renderer = ConsoleChatRenderer()
    renderer.message = rendered.append  # type: ignore[method-assign]

    renderer.on_audit_event(_answer_event(text="Kyoto"))

    (msg,) = rendered
    assert msg.kind == "user"
    assert msg.text == "Kyoto"


def test_inline_chat_renderer_renders_intervention_answer_event() -> None:
    """Tier 2: ``InlineChatRenderer.on_audit_event`` renders an
    "intervention_answer_submitted" event via ``message()`` — reachable on
    the plain-render-mode-on-a-TTY fallback (the default TTY path has its own
    TextualChatApp handler, covered above)."""
    rendered: list[OutboxMessage] = []
    renderer = InlineChatRenderer()
    renderer.message = rendered.append  # type: ignore[method-assign]

    renderer.on_audit_event(_answer_event(text="Nagoya"))

    (msg,) = rendered
    assert msg.kind == "user"
    assert msg.text == "Nagoya"
