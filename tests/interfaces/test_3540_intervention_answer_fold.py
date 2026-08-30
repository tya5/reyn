"""#3540: an answered intervention is ONE flow entry live, exactly as it is on
restore.

Owner-observed on a real TTY: answering an intervention left TWO entries — the
intervention row (``✓ answered: <label>``) plus a separate ``kind="user"`` row
carrying the same answer text — while the SAME session reloaded rendered ONE
self-contained Q→A entry (``restore.py``'s #3299 P4 shape). Two writers acted on
one answer and neither knew about the other: ``_resolve_intervention`` settled
the entry in place, and ``_handle_intervention_answer_event`` appended the
broadcast echo as its own row.

The fix reads the ``intervention_id`` the ``intervention_answer_submitted``
event has ALWAYS carried (``InterventionHandler.deliver_answer_to``) and folds
the answer into the flow entry ``announce`` already produced. The wire is
unchanged.

★ WHERE THE GATE IS AIMED. The property is **not** "the live code looks like the
restore code" — that is a proxy. It is **"live and restore produce the same
entry sequence for the same answered intervention"**, which is precisely what
the owner's report falsified, and which stays falsifiable if EITHER side drifts.
``test_live_and_restore_produce_the_same_entry_sequence`` drives BOTH sides off
ONE real ``InterventionHandler`` round trip (its announce frame + its emitted
event feed the live app; its history append feeds the restore projection), so
neither side is a hand-shaped fixture that could agree with the other by
construction.

The branch under test is on ENTRY PRESENCE, never on delivery route, so the
FALLBACK leg (an answer whose question this surface never saw — a thin client
attached after the announce) is a real, reachable leg and gets its own witness:
``test_answer_with_no_announced_entry_still_appends_a_bare_row`` asserts a row
that a dead fallback simply cannot produce, alongside an untouched unrelated
pending entry proving the lookup did not match indiscriminately.

Real instances throughout — a real ``TextualChatApp`` on a real minimal
``ClientTransport``, a real ``InterventionHandler`` / ``InterventionRegistry`` /
``SnapshotJournal`` / ``EventLog``, a real ``ReynPresenter``. No mocks, per the
testing policy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from rich.console import Console
from textual_flowview import FlowView

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter
from reyn.interfaces.inline.textual_chat.restore import project_restored_frames
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.services.intervention_handler import InterventionHandler
from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.schemas.models import Event
from reyn.user_intervention import InterventionChoice, UserIntervention

_RAW_ESC_OSC = "\x1b[31mRED\x1b]0;pwn\x07"


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time —
    display frames AND event frames, so one test can drive the announce and
    the answer through the SAME stream the app really reads."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

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

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover - no test drives the panel seam
        return True

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:  # pragma: no cover - no test drives the panel seam
        return True

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: OutboxMessage) -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _announce_frame(*, iv_id: str, prompt: str) -> OutboxMessage:
    """An announce frame shaped as ``InterventionHandler._iv_meta`` builds it."""
    return OutboxMessage(
        kind="intervention",
        text=prompt,
        meta={
            "intervention_id": iv_id,
            "intervention_kind": "confirm",
            "prompt": prompt,
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
        },
    )


def _answer_event(*, iv_id: str, text: str, meta: "dict | None" = None) -> Event:
    return Event(
        type="intervention_answer_submitted",
        data={"intervention_id": iv_id, "text": text, "meta": meta or {}},
    )


def _flow_items(app: TextualChatApp) -> "list[OutboxMessage]":
    return [e.item for e in app.query_one(FlowView).entries]


def _render(msg: OutboxMessage) -> str:
    """Render an intervention entry through the SAME presenter path a live and
    a restored entry both take."""
    presentation = ReynPresenter()._present_intervention_pending(msg, 80)
    # ``force_terminal=False``: styling escapes are the CONSOLE's, not the
    # entry's — leaving them in would both make the ESC assertions below
    # ambiguous and make the live/restore comparison depend on whether a
    # Textual app happened to be mounted when the render ran.
    console = Console(width=80, no_color=True, force_terminal=False)
    with console.capture() as cap:
        console.print(presentation.renderable)
    return cap.get()


def _signature(msg: OutboxMessage) -> "tuple[str, str]":
    """The comparable shape of one entry: its kind plus what it actually
    renders as. Compared LIVE-vs-RESTORE only — never against a literal, so
    this pins no formatting of its own.

    #5057 axis B: a live-answered entry folds to ``kind=
    "intervention_resolved"``, the SAME sibling kind restore's projection
    builds directly — both go through the ONE presenter function
    (``ReynPresenter._present_intervention_pending``, dispatched for either
    kind), so both must be rendered here too, not just "intervention"."""
    if msg.kind in ("intervention", "intervention_resolved"):
        return (msg.kind, _render(msg))
    return (msg.kind, msg.text)


# ── The symptom: pending → answered stays ONE entry ──────────────────────────


@pytest.mark.asyncio
async def test_answered_intervention_is_one_entry_pending_and_after() -> None:
    """Tier 2: an announce followed by its answer leaves exactly ONE flow
    entry, settled in place — the owner's 2→1 report, asserted at BOTH
    sections of the lifecycle.

    The WHILE-PENDING assertions are load-bearing: a build that delivered
    nothing at all would also end with "no second entry", so the pending
    section pins that the question really is on screen and unanswered before
    the answer arrives, and the answered section pins that the SAME entry (not
    a replacement) then carries the answer."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_announce_frame(iv_id="iv-1", prompt="Delete branch?"))
        await pilot.pause()

        # ── while pending ──
        pending = _flow_items(app)
        assert [m.kind for m in pending] == ["intervention"], (
            f"expected one pending intervention entry, got {[m.kind for m in pending]}"
        )
        assert (pending[0].meta or {}).get("_answer_label") is None, (
            "the entry must still be UNANSWERED before the answer event arrives"
        )
        assert "Delete branch?" in _render(pending[0])

        await transport.push_event(_answer_event(iv_id="iv-1", text="Yes"))
        await pilot.pause()

        # ── after answering ──
        answered = _flow_items(app)
        # #5057 axis B: the fold also swaps kind to "intervention_resolved"
        # (still the SAME entry object, settled in place — never a second row).
        assert [m.kind for m in answered] == ["intervention_resolved"], (
            "answering must settle the SAME entry, never append a second row; "
            f"got {[m.kind for m in answered]}"
        )
        assert (answered[0].meta or {}).get("_answer_label") == "Yes"
        rendered = _render(answered[0])
        assert "Delete branch?" in rendered and "Yes" in rendered, (
            f"the settled entry must carry BOTH question and answer: {rendered!r}"
        )


# ── The gate: live and restore agree on the entry sequence ───────────────────


def _build_handler(
    tmp_path: Path,
) -> "tuple[InterventionHandler, InterventionRegistry, list, list, list]":
    """A real, fully-wired handler whose THREE outputs are all captured:
    the announce outbox frames, the emitted audit-events, and the history
    appends — the producer both sides of the gate are driven from."""
    state_log = StateLog(tmp_path / "state.wal")
    outbox: "list[OutboxMessage]" = []
    events: "list[Event]" = []
    history: "list[dict]" = []
    event_log = EventLog(subscribers=[events.append])
    journal = SnapshotJournal(
        agent_name="test_agent", snapshot_path=tmp_path / "snap.json", state_log=state_log,
    )

    async def _put_outbox(msg: OutboxMessage) -> None:
        outbox.append(msg)

    def _append_history(
        role: str, text: str, ts: str, meta: dict, spillability=None,
    ) -> None:
        history.append({"role": role, "text": text, "meta": meta})

    handler_ref: "list[InterventionHandler]" = []

    async def _on_announce(iv: UserIntervention) -> None:
        if handler_ref:
            await handler_ref[0].announce(iv)

    registry = InterventionRegistry(on_announce=_on_announce)
    handler = InterventionHandler(
        intervention_registry=registry, journal=journal, event_log=event_log,
        put_outbox=_put_outbox, append_history=_append_history,
    )
    handler_ref.append(handler)
    return handler, registry, outbox, events, history


@pytest.mark.asyncio
async def test_live_and_restore_produce_the_same_entry_sequence(tmp_path: Path) -> None:
    """Tier 2: ★ the gate. For ONE real answered intervention, the LIVE entry
    sequence and the RESTORED entry sequence are the same — same kinds, same
    rendered content, same length.

    Both sides come from the SAME real ``InterventionHandler`` round trip: its
    ``announce`` outbox frame and its ``intervention_answer_submitted`` event
    drive the live app; the history record it appended drives
    ``project_restored_frames``. That is what makes disagreement (the owner's
    report: two entries live, one restored) detectable from EITHER side —
    implementation sameness is not asserted anywhere, because it is a proxy
    for this, not the property itself."""
    from tests._async_wait import wait_until  # noqa: PLC0415 — shared #1751 test helper

    handler, registry, outbox, events, history = _build_handler(tmp_path)
    iv = UserIntervention(
        kind="ask_user",
        prompt="Delete the branch?",
        run_id="run-1",
        choices=[
            InterventionChoice(id="yes", label="Yes", hotkey="y"),
            InterventionChoice(id="no", label="No", hotkey="n"),
        ],
    )
    dispatch_task = asyncio.ensure_future(handler.dispatch(iv))
    await wait_until(lambda: bool(registry.list_active()))
    assert await handler.deliver_answer_to(iv, "", choice_id_override="yes") is True
    await asyncio.gather(dispatch_task, return_exceptions=True)

    announces = [m for m in outbox if m.kind == "intervention"]
    answer_events = [e for e in events if e.type == "intervention_answer_submitted"]
    assert [m.kind for m in announces] == ["intervention"], (
        f"producer must announce exactly once; outbox={[m.kind for m in outbox]}"
    )
    assert [e.type for e in answer_events] == ["intervention_answer_submitted"], (
        f"producer must broadcast exactly one answer; events={[e.type for e in events]}"
    )

    # ── live side ──
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(announces[0])
        await pilot.pause()
        await transport.push_event(answer_events[0])
        await pilot.pause()
        live = [_signature(m) for m in _flow_items(app)]

    # ── restore side ──
    restored_msgs = [
        ChatMessage(role=h["role"], content=h["text"], meta=h["meta"]) for h in history
    ]
    restored = [
        _signature(f)
        for f in project_restored_frames(restored_msgs)
        if f.kind != "system"
    ]

    assert live == restored, (
        "live and restore must render the same entry sequence for the same "
        f"answered intervention; live={live!r} restored={restored!r}"
    )
    # Non-vacuity: both sides must actually SHOW the answered Q→A — two empty
    # sequences would otherwise compare equal.
    # #5057 axis B: both sides settle to the resolved sibling kind.
    assert [kind for kind, _ in live] == ["intervention_resolved"], (
        f"expected exactly the answered Q→A entry on both sides, got {live!r}"
    )
    assert "Delete the branch?" in live[0][1] and "Yes" in live[0][1]


# ── The fallback leg — reached, and asserted on a value a dead leg can't make ─


@pytest.mark.asyncio
async def test_answer_with_no_announced_entry_still_appends_a_bare_row() -> None:
    """Tier 2: ★ fallback witness. An answer whose intervention this surface
    never announced (a thin client attached AFTER the announce) still renders
    — as the bare ``kind="user"`` row it always did, because there is no
    question entry to fold into.

    A fallback nobody reaches stays green while dead, so this asserts a value
    the dead leg cannot produce: the ``kind="user"`` row itself, which exists
    ONLY if the append actually ran. The unrelated pending intervention in the
    same flow is asserted UNTOUCHED, which is what distinguishes "the lookup
    found nothing and fell back" from "the lookup matched anything at hand"."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_announce_frame(iv_id="iv-seen", prompt="Unrelated?"))
        await pilot.pause()
        await transport.push_event(
            _answer_event(iv_id="iv-never-announced", text="Osaka", meta={"actor": "bob"})
        )
        await pilot.pause()

        items = _flow_items(app)
        assert [m.kind for m in items] == ["intervention", "user"], (
            "an uncorrelated answer must still reach the flow as its own row; "
            f"got {[m.kind for m in items]}"
        )
        assert items[1].text == "Osaka"
        assert (items[1].meta or {}).get("actor") == "bob", (
            "the fallback leg keeps the answer's attribution meta (ADR-0039)"
        )
        assert (items[0].meta or {}).get("_answer_label") is None, (
            "the unrelated pending intervention must NOT have been settled"
        )


@pytest.mark.asyncio
async def test_folded_answer_label_is_neutralized_at_the_presenter_boundary() -> None:
    """Tier 2: the folded answer keeps the wire's RAW bytes (the same thing a
    persisted answer keeps, which is what lets live and restore compare equal)
    and is stripped at the ONE shared display boundary —
    ``ReynPresenter._present_intervention_pending`` — so an ESC/OSC payload in
    a model-supplied choice label cannot reach the TTY."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_announce_frame(iv_id="iv-1", prompt="Which?"))
        await pilot.pause()
        await transport.push_event(_answer_event(iv_id="iv-1", text=_RAW_ESC_OSC))
        await pilot.pause()

        (item,) = _flow_items(app)
        rendered = _render(item)
        assert "\x1b" not in rendered and "\x07" not in rendered, (
            f"raw ESC/BEL leaked into the settled answer line: {rendered!r}"
        )
        assert "RED" in rendered
