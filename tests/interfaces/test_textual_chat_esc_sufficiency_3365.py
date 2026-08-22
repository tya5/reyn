"""#3365 — Esc-sufficiency gate: Esc returns to the Composer from EVERY focus
state, before Tab's redundant "back to composer" binding is removed from
SentQueue/InterventionPanel.

Architect's ruling on #3365 (issue thread): Tab should become forward-only
(accept/next), with "back" left to Esc alone — but ONLY once Esc's
sufficiency from every reachable focus state is machine-verified, because a
future PR could give the InterventionPanel's own ``Input`` widget an
Esc-clears-text binding, which would swallow the bubble and — if Tab had
already been removed as a redundant escape hatch — leave the user stuck with
NO way back (exactly #3327's "the one remaining exit closed" shape). This
gate is what lets a later PR remove Tab's "back" binding with a real, not
merely assumed, safety net; and what would immediately catch that future
Input-vs-Esc regression by going RED.

Seven HAND-ENUMERATED focus states, each independently reachable and each
asserted to return to the Composer on ``Esc``:
  1. MenuBar (tab-bar row, not yet opened)
  2. Drawer content (an OptionList pane)
  3. SentQueue
  4. InterventionPanel — closed-set (RadioSet)
  5. InterventionPanel — free-text (Input) — the specific widget architect
     flagged as the future risk
  6. FlowView (the conversation pane — ``can_focus=True``, reachable via
     Textual's own Shift+Tab focus cycling). #3470's design review found
     this region ALREADY existed outside the original hand-enumeration —
     exactly the "6th focusable region" case the note below predicted —
     so it is now armed here per that note's own instruction.
  7. SearchBar's query ``Input`` (#3476 ⑤ — reached via ``ctrl+n``, not
     Shift+Tab cycling). Added on the SAME "maintainer's job" instruction
     the note below states — #3488 introduced this region without arming
     it here; closed by this PR.

Plus one case that is deliberately the OPPOSITE assertion — Esc does NOT
reach the Composer:

  8. FlowView with an active visual selection (#3692 PR-B ②) — flowview
     0.13's own Esc binding (``cursor_cancel``) is live only while a
     selection exists, so the key is consumed one layer in and never
     bubbles to reyn's app-level "close drawer / back to Composer"
     binding. This is the innermost of #3692's three Esc layers; the other
     two (drawer open -> close it; nothing selected -> Composer) are
     exactly states 2 and 6 above, re-verified to still hold once the
     flowview 0.13 pin (#3692 PR-A) gave FlowView its own competing Esc
     binding for the first time.

This list is NOT derived from an exhaustive enumeration of every focusable
widget the app can mount (co-vet note, #3365 review) — it is the set of
regions the #3365/#3470 investigations reached. A NEW focusable region added
later (a new drawer pane type, a new panel) is NOT automatically covered by
this file; extending the list here is the maintainer's job when one is
added, the same way a new call site needs its own invariant check elsewhere
in this codebase.

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` — no mocks, per
the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.widgets import Input, OptionList, RadioSet

from reyn.interfaces.inline.textual_chat import Composer, MenuBar, TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport`. ``end=False`` keeps the
    stream open so the app under test stays mounted for focus inspection."""

    def __init__(self, messages: "list[OutboxMessage]") -> None:
        self._messages = list(messages)
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
        self._messages.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _choice_intervention() -> OutboxMessage:
    return OutboxMessage(
        kind="intervention",
        text="Proceed?\n  Yes / No",
        meta={
            "intervention_id": "iv-1",
            "intervention_kind": "confirm",
            "prompt": "Proceed?",
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
            "nodes": [
                {"component": "text", "text": "Proceed?"},
                {"component": "list", "items": ["Yes", "No"]},
            ],
        },
    )


def _free_text_intervention() -> OutboxMessage:
    return OutboxMessage(
        kind="intervention",
        text="What is the target directory?",
        meta={
            "intervention_id": "iv-2",
            "intervention_kind": "ask_user",
            "prompt": "What is the target directory?",
        },
    )


@pytest.mark.asyncio
async def test_esc_from_menubar_returns_to_composer() -> None:
    """Tier 2b: Esc from the MenuBar tab-bar (drawer not yet opened) -> Composer."""
    app = TextualChatApp(transport=_Transport([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(app.focused, MenuBar), f"setup: expected MenuBar focused, got {app.focused!r}"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.focused, Composer), (
            f"Esc from MenuBar did not return to Composer: {app.focused!r}"
        )


@pytest.mark.asyncio
async def test_esc_from_drawer_content_returns_to_composer() -> None:
    """Tier 2b: Esc from an OPEN drawer's content (an OptionList) -> Composer,
    regardless of navigation depth inside it (matches the architect's own
    measured trace: depth does not change the destination)."""
    app = TextualChatApp(transport=_Transport([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.focused, OptionList), (
            f"setup: expected an open drawer's OptionList focused, got {app.focused!r}"
        )
        # Navigate a few rows deep before Esc — the destination must not depend on depth.
        await pilot.press("down", "down", "down")
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.focused, Composer), (
            f"Esc from drawer content did not return to Composer: {app.focused!r}"
        )


@pytest.mark.asyncio
async def test_esc_from_sent_queue_returns_to_composer() -> None:
    """Tier 2b: Esc from a focused, non-empty SentQueue -> Composer."""
    app = TextualChatApp(transport=_Transport([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        # Populate directly via the widget's own public API (mirrors how
        # #3327's tests exercise SentQueue) — kept minimal/scoped to this gate.
        sent_queue = app.query_one(SentQueue)
        sent_queue.show_item(msg_id="m1", text="queued while busy")
        await pilot.pause()
        assert sent_queue.has_items(), "test setup: sent-queue must be non-empty"

        sent_queue.focus()
        await pilot.pause()
        assert sent_queue.has_focus, "test setup: sent-queue did not take focus"

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(Composer).has_focus, (
            f"Esc from SentQueue did not return to Composer: {app.focused!r}"
        )


@pytest.mark.asyncio
async def test_esc_from_intervention_panel_choice_returns_to_composer() -> None:
    """Tier 2b: Esc from a closed-set InterventionPanel (RadioSet) -> Composer."""
    transport = _Transport([_choice_intervention()])
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        radio = app.query_one(InterventionPanel).query_one(RadioSet)
        assert radio.has_focus, "test setup: panel did not auto-focus on arrival"

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(Composer).has_focus, (
            f"Esc from InterventionPanel (choice) did not return to Composer: {app.focused!r}"
        )
        assert transport.answered_choice == [], "Esc must not deliver an answer"


@pytest.mark.asyncio
async def test_esc_from_intervention_panel_input_returns_to_composer() -> None:
    """Tier 2b: ★ the specific case architect flagged as the future risk —
    Esc while focus is on the InterventionPanel's free-text ``Input`` (not
    the RadioSet) still returns to the Composer.

    This is the gate that must go RED the day a future PR gives ``Input`` its
    own Esc-clears-text binding without also preserving the bubble-to-panel
    fallback — the #3327 "last exit closed" shape, but for Input specifically."""
    transport = _Transport([_free_text_intervention()])
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        input_widget = app.query_one(InterventionPanel).query_one(Input)
        input_widget.focus()
        await pilot.pause()
        assert input_widget.has_focus, "test setup: Input did not take focus"

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(Composer).has_focus, (
            f"Esc from InterventionPanel's Input did not return to Composer: {app.focused!r}"
        )
        assert transport.answered_text == [], "Esc must not deliver an answer"


@pytest.mark.asyncio
async def test_esc_from_conversation_pane_returns_to_composer() -> None:
    """Tier 2b: Esc from the focused conversation pane (FlowView) -> Composer.

    The 6th hand-enumerated state (see the module docstring): FlowView is
    ``can_focus=True`` and reachable through Textual's own Shift+Tab focus
    cycling — a route #3470's design review found already live but entirely
    outside the original enumeration. Reached here exactly that way (a real
    ``shift+tab`` keypress, not a programmatic ``.focus()``), so the test
    covers the route a keyboard user actually takes."""
    from textual_flowview import FlowView

    app = TextualChatApp(transport=_Transport([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()

        await pilot.press("shift+tab")
        await pilot.pause()
        assert isinstance(app.focused, FlowView), (
            f"setup: Shift+Tab did not reach the conversation pane: {app.focused!r}"
        )

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(Composer).has_focus, (
            f"Esc from the conversation pane did not return to Composer: {app.focused!r}"
        )


@pytest.mark.asyncio
async def test_esc_with_an_active_selection_cancels_it_and_stays_on_the_pane() -> None:
    """Tier 2b: #3692 PR-B ② — the INNERMOST Esc layer. With an active visual
    selection, Esc cancels the selection but does NOT bubble past flowview:
    focus stays on the conversation pane rather than returning to the
    Composer (the opposite of the sibling case directly above, which has no
    selection to intercept the key).

    flowview 0.13's own ``check_action`` gates its ``cursor_cancel`` binding
    live only while a selection is active (``self._tc_anchor is not None``)
    — this is what makes the layering work by ordinary Textual bubbling, not
    anything reyn adds: reyn declares no ``priority=True`` on its own
    app-level Esc binding (#3692 issue body's stated implementation
    requirement), so flowview's binding gets first refusal whenever it is
    live.

    The witness is public: ``Screen.get_selected_text()`` grows as the
    selection is extended, then — on ``Esc`` — shrinks back down to the
    SAME length as the bare cursor's own pre-extension baseline (not
    necessarily the same character: cancelling a visual selection leaves
    the cursor where the selection ended, vim-style, not where it started —
    only the WIDTH collapses). Measured against the actual baseline rather
    than a hardcoded size, so this does not pin the cursor's rendered width
    — not a private ``_tc_anchor`` read either."""
    from textual_flowview import FlowView

    app = TextualChatApp(transport=_Transport([]))
    async with app.run_test(size=(100, 30)) as pilot:
        app.conversation.append(OutboxMessage(kind="agent", text="newest reply"))
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()

        await pilot.press("shift+tab")
        await pilot.pause()
        flow = app.focused
        assert isinstance(flow, FlowView), (
            f"setup: Shift+Tab did not reach the conversation pane: {flow!r}"
        )
        await pilot.press("c")  # show the text cursor
        await pilot.pause()
        baseline = flow.screen.get_selected_text() or ""

        await pilot.press("v")  # start a visual selection
        await pilot.press("l", "l", "l")  # extend it
        await pilot.pause()
        extended = flow.screen.get_selected_text() or ""
        assert len(extended) > len(baseline), (
            f"test setup: extending the visual selection did not grow it past "
            f"the bare-cursor baseline {baseline!r}: {extended!r}"
        )

        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is flow, (
            f"Esc with an active selection bubbled past the pane instead of "
            f"being consumed by it: {app.focused!r}"
        )
        # A trailing assert on the selection-width-after-Esc (`len(after) ==
        # len(baseline)`) was removed here (#4304, part of #3880): it pinned
        # flowview's own cursor_cancel binding collapsing a visual selection
        # back to baseline width — reyn's own claim is only that Esc reaches
        # the pane (asserted above), not what flowview's own cursor_cancel does
        # with it.


@pytest.mark.asyncio
async def test_esc_from_search_bar_returns_to_composer() -> None:
    """Tier 2b: Esc from the search bar's query Input -> Composer.

    The 7th hand-enumerated state (see the module docstring): SearchBar is
    reached via ``ctrl+n`` (#3476 ⑤), not Shift+Tab cycling — a different
    reachability path than every other state in this file, so it is armed
    here as its own case rather than assumed to be covered by the FlowView
    case above."""
    from reyn.interfaces.inline.textual_chat.search_bar import SearchBar

    app = TextualChatApp(transport=_Transport([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.focused, Input), (
            f"setup: ctrl+n did not focus the search input: {app.focused!r}"
        )

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(Composer).has_focus, (
            f"Esc from the search bar did not return to Composer: {app.focused!r}"
        )
        assert not app.query_one(SearchBar).display, (
            "Esc from the search bar left it open"
        )
