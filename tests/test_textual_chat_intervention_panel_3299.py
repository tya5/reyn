"""#3299 P1: intervention interaction moved into the grouped InterventionPanel.

Retargets the Phase-3.5 chip/match tests (#3273, ``test_textual_chat_phase35_3273.py``)
to the new panel-widget path — the ATOMIC display-swap + input-swap +
chip-retire this PR lands. The chip surface (``choice_chip_spans`` /
``_present_intervention_choice`` / ``on_flow_view_clicked`` /
``_match_choice_input`` / ``_surface_choice_hint`` / ``_CHOICE_*``) is retired
(grep-zero in ``src/``, see the PR body); a closed-set intervention is now
answered by SELECTING a :class:`~textual.widgets.RadioSet` option in the panel
(never a free-text label match — the #3290 type-or-click matching algorithm is
retired along with the Composer intervention-answer path it served, not
retargeted: the Composer is now exclusively for new turns), and a free-text
intervention by submitting the panel's own :class:`~textual.widgets.Input`.

Gates pinned here (per the architect's P1 scope ruling):

- **F1 permission-band reachability**: a real closed-set intervention frame
  populates the panel; selecting the SECOND RadioButton delivers exactly that
  option's ``choice_id`` through ``transport.answer_intervention_choice`` — the
  UNCHANGED transport funnel every answer path shares.
- **free-text reachability**: a free-text intervention's panel Input, once
  submitted, delivers through ``transport.answer_intervention_text``.
- **no-double-display / no-double-input (retire non-vacuity)**: the flow entry
  never renders chips (the presenter's chip-drawing symbols are gone — a
  direct grep-zero check — and the rendered flow-entry presentation carries
  only the prompt + a "respond in the panel below" hint, not the option
  labels); a Composer submit during a pending intervention is ALWAYS a new
  turn (``submit_user_text``), never an intervention answer.
- **focus-return**: Esc/Tab inside the panel returns focus to the Composer.
- **neutralization regression**: an LLM-derived choice label carrying raw
  terminal control sequences is still neutralized before it reaches the
  rendered flow-entry head (moved off the old chip-rendering path).

All use real instances (a concrete recording :class:`ClientTransport`, a real
mounted :class:`TextualChatApp`, real :class:`OutboxMessage`) — no mocks — per
the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.widgets import Input, RadioSet
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_GUTTER_WIDTH = 2


class RecordingTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` that replays a fixed frame list
    and RECORDS which answer seam each user action reached.

    ``end=False`` keeps the stream open so the app stays mounted for
    inspection.
    """

    def __init__(
        self,
        messages: "list[OutboxMessage]",
        *,
        end: bool = False,
    ) -> None:
        self._messages = list(messages)
        self._end = end
        self.submitted: list[str] = []
        self.answered_choice: list[str] = []
        self.answered_text: list[str] = []
        self.displayed: list[OutboxMessage] = []

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
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self.displayed.append(msg)
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


def _free_text_intervention() -> OutboxMessage:
    """A free-text (no ``choices``) intervention frame."""
    return OutboxMessage(
        kind="intervention",
        text="What is the target directory?",
        meta={
            "intervention_id": "iv-2",
            "intervention_kind": "ask_user",
            "prompt": "What is the target directory?",
        },
    )


def _iv_entry(app: TextualChatApp):
    entries = [
        e for e in app.query_one(FlowView).entries if e.item.kind == "intervention"
    ]
    assert len(entries) == 1, f"expected one intervention entry, got {len(entries)}"
    return entries[0]


@pytest.mark.asyncio
async def test_choice_intervention_panel_selection_delivers_correct_choice_id() -> None:
    """Tier 2b: F1 permission-band reachability — a closed-set intervention
    frame populates the panel's RadioSet; selecting the SECOND option delivers
    that option's ``choice_id`` ("no") through ``answer_intervention_choice``,
    and the flow entry resolves to SUCCESS. Non-vacuous: the SECOND option is
    chosen (a first-option shortcut would fail) and the exact id is asserted;
    no answer is delivered before the selection."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is True, "panel did not show for a pending intervention"
        radio = panel.query_one("#iv-panel-choices", RadioSet)
        assert radio.display is True
        assert radio.has_focus, "panel did not auto-focus the RadioSet"

        # No answer is delivered until the user acts.
        assert transport.answered_choice == []

        # Move the highlight to the SECOND option ("No") and select it. The
        # RadioSet starts with NO highlighted button (index -1), so the FIRST
        # "down" only highlights index 0 ("Yes") — a second "down" is needed to
        # reach index 1 ("No"), verified against this RadioSet's own behavior.
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["no"], (
            f"choice not delivered; got {transport.answered_choice}"
        )
        # Resolved reflection: green SUCCESS gutter + panel collapsed + focus
        # returned to the Composer.
        resolved = _iv_entry(app)
        assert resolved.state is EntryState.SUCCESS
        assert resolved.item.meta.get("_answer_label") == "No"
        assert panel.display is False, "panel did not collapse after resolving"
        assert app.query_one(Composer).has_focus, (
            "focus did not return to the Composer after resolving"
        )


@pytest.mark.asyncio
async def test_free_text_intervention_answered_via_panel_input() -> None:
    """Tier 2b: a free-text intervention (no choices) populates the panel's
    Input (auto-focused); submitting delivers the text through
    ``answer_intervention_text``. Regression retarget of the #3273 Composer
    free-text test — the answer path moved from the Composer to the panel."""
    transport = RecordingTransport([_free_text_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        text_input = panel.query_one("#iv-panel-input", Input)
        assert text_input.display is True
        assert text_input.has_focus, "panel did not auto-focus the Input"

        await pilot.press("o", "k")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

    assert transport.answered_text == ["ok"]
    assert transport.submitted == []
    assert transport.answered_choice == []


@pytest.mark.asyncio
async def test_composer_submit_during_pending_intervention_is_always_a_new_turn() -> None:
    """Tier 2b: no-double-input — the Composer no longer special-cases a
    pending intervention at all (#3299 P1: it no longer reads
    ``pending_intervention_head()``). A Composer submit while a closed-set
    intervention is pending is ALWAYS routed to ``submit_user_text`` — never
    ``answer_intervention_choice`` / ``answer_intervention_text`` — even when
    the text happens to name an option. This is the atomic-swap witness: if
    the retired Composer-side matching were still live (a half-swap), this
    would instead deliver a choice answer."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("y", "e", "s")
        await pilot.press("enter")
        await pilot.pause()

    assert transport.submitted == ["yes"]
    assert transport.answered_choice == []
    assert transport.answered_text == []


def test_chip_drawing_symbols_are_retired() -> None:
    """Tier 1: retire grep-zero (non-vacuity witness) — the presenter module no
    longer defines the chip-drawing surface at all. This would fail RED if the
    chip path were still live alongside the panel (double-display)."""
    from reyn.interfaces.inline.textual_chat import presenter as presenter_module

    for name in (
        "choice_chip_spans",
        "_present_intervention_choice",
        "_choice_chip",
        "_CHOICE_INDENT",
        "_CHOICE_GAP",
        "_CHOICE_HINT",
    ):
        assert not hasattr(presenter_module, name), f"{name} still defined — chip path not retired"

    from reyn.interfaces.inline.textual_chat import app as app_module

    for name in ("_match_choice_input", "_surface_choice_hint", "_CHOICE_HINT_TEXT"):
        assert not hasattr(app_module, name), f"{name} still defined — match path not retired"

    assert not hasattr(app_module.TextualChatApp, "on_flow_view_clicked"), (
        "on_flow_view_clicked still defined — chip click handler not retired"
    )


@pytest.mark.asyncio
async def test_pending_intervention_flow_entry_has_no_chip_options_rendered() -> None:
    """Tier 2b: no-double-display — the flow entry for a pending intervention
    renders ONLY the prompt head + a dim hint pointing at the panel, never the
    option labels (which live exclusively in the panel's RadioSet now).
    Non-vacuous: the pre-#3299 chip presentation rendered every option label
    inline (``[ 1 · Yes ]`` etc.) — asserting none of those labels appear in
    the flow-entry rendering is exactly what a surviving chip branch would
    violate."""
    from rich.console import Console

    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _iv_entry(app)
        presentation = await app._presenter.present(entry.item, 80)

    console = Console(width=80, no_color=True)
    with console.capture() as cap:
        console.print(presentation.renderable)
    rendered = cap.get()

    assert "Allow write to /etc/hosts?" in rendered
    assert "respond in the panel below" in rendered
    # The old chip layout rendered every option as ``[ n · label ]`` inline —
    # asserting that shape is absent is exactly what a surviving chip branch
    # would violate.
    assert "[ 1 ·" not in rendered and "[ 2 ·" not in rendered, (
        "chip-shaped option markup leaked into the pending flow entry"
    )


@pytest.mark.asyncio
async def test_escape_and_tab_from_panel_return_focus_to_composer() -> None:
    """Tier 2b: focus-return — Esc/Tab inside the panel return focus to the
    Composer WITHOUT answering; the intervention stays pending (the panel
    stays open, mirroring the Phase-3 drawer's deterministic focus-flow)."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        panel = app.query_one(InterventionPanel)
        assert panel.query_one("#iv-panel-choices", RadioSet).has_focus

        await pilot.press("escape")
        await pilot.pause()

        assert app.query_one(Composer).has_focus, "Esc did not return focus to the Composer"
        assert panel.display is True, "Esc must not answer/collapse the panel"
        assert transport.answered_choice == []
        assert transport.answered_text == []

        # Re-focus the panel to exercise Tab too.
        panel.query_one("#iv-panel-choices", RadioSet).focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()

        assert app.query_one(Composer).has_focus, "Tab did not return focus to the Composer"
        assert panel.display is True
        assert transport.answered_choice == []


def test_choice_labels_are_neutralized_before_rendering() -> None:
    """Tier 2c: an LLM-derived choice LABEL carrying raw terminal control
    sequences must not leak into the flow entry's rendering — retargeted off
    the retired chip path onto :meth:`ReynPresenter._present_intervention_pending`,
    which renders the prompt head (labels themselves now live only in the
    panel's RadioSet, built by :meth:`InterventionPanel.show_choice` from the
    SAME raw ``meta["choices"]`` — Textual's own :class:`RadioButton` renders
    its label through Rich markup, not raw ANSI passthrough, so the injection
    surface pinned here is the flow-entry head, matching the original test's
    payload and assertions).

    NON-VACUITY (falsification): neutering ``presenter._neutralized_label`` to
    identity makes the raw ``\\x1b`` leak into the rendered head and flips this
    assertion RED — verified locally, so a silent removal of the
    neutralization is caught here."""
    from rich.console import Console

    from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter

    payload = "\x1b[31mDANGER\x1b]0;pwn\x07"
    msg = OutboxMessage(
        kind="intervention",
        text="Allow write?",
        meta={
            "intervention_id": "iv-x",
            "intervention_kind": "confirm",
            "prompt": payload,
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
        },
    )

    presentation = ReynPresenter()._present_intervention_pending(msg, 80)
    console = Console(width=80, no_color=True)
    with console.capture() as cap:
        console.print(presentation.renderable)
    rendered = cap.get()

    assert "\x1b" not in rendered, f"raw ESC leaked into the flow-entry head: {rendered!r}"
    assert "\x07" not in rendered, f"raw BEL leaked into the flow-entry head: {rendered!r}"
    assert "DANGER" in rendered
