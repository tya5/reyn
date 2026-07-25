"""Phase 3.5 TUI-rebuild gates (#3273): choice-intervention reachability.

These pin the Phase-3.5 fix (ADR self-review finding F1): a closed-set
intervention (permission confirm / choice ``ask_user`` — any
``kind="intervention"`` frame carrying ``meta["choices"]``) is REACHABLE in the
Textual TTY. The free-text-only wiring left choice interventions unanswerable
(the only ``answer_intervention_choice`` caller lived in the dead old app), a
permission-band functional regression this restores.

Gates:

- **choice REACHABLE** (Tier 2b): a choice-intervention frame surfaces as
  in-flow option chips; a click on a chip delivers the CORRECT ``choice_id``
  through ``transport.answer_intervention_choice`` and the entry re-presents to
  its resolved (``EntryState.SUCCESS``) state. Non-vacuous: the second option is
  chosen (so a first-option shortcut would fail), the specific id is asserted,
  and the SAME pending choice-intervention is shown to be UNANSWERABLE via the
  free-text composer path (which starts a new turn instead) — the pre-fix gap.
- **free-text still works** (Tier 2b): a no-choices intervention still routes a
  composer submit to ``answer_intervention_text`` (regression guard).

All use real instances (a concrete recording :class:`ClientTransport`, a real
mounted :class:`TextualChatApp`, real :class:`OutboxMessage`) — no mocks — per
the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import (
    Composer,
    TextualChatApp,
    choice_chip_spans,
)
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_GUTTER_WIDTH = 2


class _FreeTextHead:
    """A pending free-text intervention head (no ``choices`` attr) — the shape
    the local transport's ``pending_intervention_head`` returns for an
    ``ask_user`` / secret prompt that the composer answers as text."""


class RecordingTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` that replays a fixed frame list
    and RECORDS which answer seam each user action reached.

    ``end=False`` keeps the stream open so the app stays mounted for inspection.
    ``head`` is the pending-intervention head the free-text ``_submit`` path
    reads (``None`` = no pending intervention → a submit is a new turn).
    """

    def __init__(
        self,
        messages: "list[OutboxMessage]",
        *,
        end: bool = False,
        head: "object | None" = None,
    ) -> None:
        self._messages = list(messages)
        self._end = end
        self._head = head
        self.submitted: list[str] = []
        self.answered_choice: list[str] = []
        self.answered_text: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        if self._end:
            yield DisplayFrame(OutboxMessage(kind="__end__", text=""))
        else:
            await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        self.answered_text.append(text)
        return True

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        self.answered_choice.append(choice_id)
        return True

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return self._head

    def put_display(self, msg: "OutboxMessage") -> None:
        self._messages.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _choice_intervention() -> OutboxMessage:
    """A closed-set (permission-confirm) intervention frame, shaped exactly like
    ``session._iv_meta`` builds it: structured ``prompt`` + ``choices`` (id /
    label / hotkey) plus the ``nodes`` render-model."""
    return OutboxMessage(
        kind="intervention",
        text="Allow write to /etc/hosts?\n  Yes / No / Always",
        meta={
            "intervention_id": "iv-1",
            "intervention_kind": "confirm",
            "prompt": "Allow write to /etc/hosts?",
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
                {"id": "always", "label": "Always", "hotkey": "A"},
            ],
            "nodes": [
                {"component": "text", "text": "Allow write to /etc/hosts?"},
                {"component": "list", "items": ["Yes", "No", "Always"]},
            ],
        },
    )


def _iv_entry(app: TextualChatApp):
    entries = [
        e for e in app.query_one(FlowView).entries if e.item.kind == "intervention"
    ]
    assert len(entries) == 1, f"expected one intervention entry, got {len(entries)}"
    return entries[0]


@pytest.mark.asyncio
async def test_choice_intervention_click_delivers_correct_choice_id() -> None:
    """Tier 2b: a choice-intervention frame is REACHABLE — clicking its second
    option chip delivers that option's ``choice_id`` ("no") through
    ``answer_intervention_choice`` and the entry goes to its resolved SUCCESS
    state. This is the F1 permission-band reachability witness. Non-vacuous: the
    SECOND option is chosen and its exact id asserted (a first-option or
    label-vs-id confusion fails), and no answer is delivered before the click."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _iv_entry(app)
        flow = app.query_one(FlowView)

        # No answer is delivered until the user acts.
        assert transport.answered_choice == []

        body_width = max(1, flow.scrollable_content_region.width - _GUTTER_WIDTH)
        chip_row = app._presenter.choice_chip_row(entry.item, body_width)
        spans = choice_chip_spans(entry.item.meta["choices"])
        # Choose the SECOND chip ("No", id "no") — proves the click maps to the
        # right choice, not merely "some choice was answered".
        start, end, choice_id = spans[1]
        assert choice_id == "no"
        click_x = (start + end) // 2
        app.post_message(FlowView.Clicked(flow, entry, click_x, chip_row))
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["no"], (
            f"choice not delivered; got {transport.answered_choice}"
        )
        # Resolved reflection: green SUCCESS gutter + recorded chosen label.
        resolved = _iv_entry(app)
        assert resolved.state is EntryState.SUCCESS
        assert resolved.item.meta.get("_chosen_label") == "No"


@pytest.mark.asyncio
async def test_choice_click_off_the_chip_row_does_not_answer() -> None:
    """Tier 2b: hit-testing is non-vacuous — a click on the prompt HEAD row (not
    the chip row) delivers nothing, so the reachability test's positive result is
    attributable to landing on a chip, not to any click resolving the whole
    entry."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _iv_entry(app)
        flow = app.query_one(FlowView)
        # Row 0 is the prompt head, never the chip row (the head is one line here).
        app.post_message(FlowView.Clicked(flow, entry, 3, 0))
        await pilot.pause()
        await pilot.pause()

    assert transport.answered_choice == []


@pytest.mark.asyncio
async def test_choice_intervention_unanswerable_via_free_text_path() -> None:
    """Tier 2b: the pre-fix gap witness — a pending CHOICE intervention is NOT
    answered by the free-text composer path. With the choice-intervention head
    pending, a composer submit starts a NEW TURN (``submit_user_text``) rather
    than answering the intervention, so without the click wiring the choice would
    never be delivered. Pairs with the reachability test to show the click path
    is the only thing that closes the gap."""

    class _ChoiceHead:
        choices = [object(), object()]  # non-empty → NOT a free-text intervention

    transport = RecordingTransport(
        [_choice_intervention()], end=False, head=_ChoiceHead()
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()

    # A choice-intervention is pending, yet the text submit went to a new turn,
    # NOT to any intervention-answer seam.
    assert transport.submitted == ["hi"]
    assert transport.answered_choice == []
    assert transport.answered_text == []


@pytest.mark.asyncio
async def test_free_text_intervention_still_answered_via_composer() -> None:
    """Tier 2b: regression — a FREE-TEXT intervention (no choices) still routes a
    composer submit to ``answer_intervention_text``, unchanged by the choice
    wiring. The head has no ``choices`` attr, so ``_submit`` takes the answer
    path."""
    transport = RecordingTransport([], end=False, head=_FreeTextHead())
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("o", "k")
        await pilot.press("enter")
        await pilot.pause()

    assert transport.answered_text == ["ok"]
    assert transport.submitted == []
    assert transport.answered_choice == []


def test_choice_labels_are_neutralized_before_rendering() -> None:
    """Tier 2c: an LLM-derived choice LABEL carrying raw terminal control
    sequences is neutralized before it reaches the rendered chip cells — a
    terminal-escape-injection guard on a permission surface.

    Choice labels reach ``meta["choices"]`` RAW (``session._iv_meta`` copies
    ``choice.label`` verbatim; only the ``nodes`` render-model is neutralized at
    source), so the presenter MUST strip control bytes at its own boundary. This
    builds a choice-intervention whose label embeds a CSI colour + OSC
    title-set + bare ESC/BEL, presents it through the real
    ``_present_intervention_choice`` path, renders the presentation through a
    no-colour Console (so any escape in the output came from the LABEL, not from
    Rich styling), and asserts the rendered cells carry NO raw ``\\x1b`` / ``\\x07``
    while the visible label text survives (neutralized, not dropped).

    NON-VACUITY (falsification): neutering ``presenter._neutralized_label`` to
    identity (the future refactor the co-vet flagged) makes the raw ``\\x1b`` leak
    into the rendered cells and flips this assertion RED — verified locally, so a
    silent removal of the neutralization is caught here."""
    from rich.console import Console

    from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter

    payload = "\x1b[31mDANGER\x1b]0;pwn\x07"
    msg = OutboxMessage(
        kind="intervention",
        text="Allow write?",
        meta={
            "intervention_id": "iv-x",
            "intervention_kind": "confirm",
            "prompt": "Allow write to /etc/hosts?",
            "choices": [
                {"id": "yes", "label": payload, "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
        },
    )

    presentation = ReynPresenter()._present_intervention_choice(msg, 80)
    console = Console(width=80, no_color=True)
    with console.capture() as cap:
        console.print(presentation.renderable)
    rendered = cap.get()

    assert "\x1b" not in rendered, f"raw ESC leaked into chip cells: {rendered!r}"
    assert "\x07" not in rendered, f"raw BEL leaked into chip cells: {rendered!r}"
    # The visible label survives — neutralization strips the control bytes, it
    # does not drop the option (a dropped option would be its own regression).
    assert "DANGER" in rendered
