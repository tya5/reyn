"""#3299 P1/P2/P5 (#3308): intervention interaction in a TAB-IFIED grouped panel.

Retargets the Phase-3.5 chip/match tests (#3273) to the panel-widget path
(P1), then the P2 by-id multi-pending fixes, then #3308 (P5)'s tab-ify: one
:class:`~textual.widgets.TabPane` per PENDING intervention instead of a
single re-routing form. The chip surface is retired (grep-zero, unchanged
since P1); a closed-set intervention is answered by SELECTING a
:class:`~textual.widgets.RadioSet` option in its OWN tab, and a free-text one
by submitting its OWN tab's :class:`~textual.widgets.Input`.

Gates pinned here (#3308 acceptance conditions, numbered to match the issue):

1. **Enter-twice delivers exactly one answer** — answering a tab does not
   change the active tab, so a muscle-memory second ``Enter`` lands on the
   SAME (now ✓-answered, inert) tab and delivers nothing to an unread other
   pending intervention. (Migrates the P2 F1-interim test's property (a),
   per the #3308 co-vet correction: the interim MECHANISM — re-route
   suppressing pre-highlight — is retired, but this SAFETY PROPERTY is not.)
2. **A new arrival never steals the active tab** — Textual's own
   ``TabbedContent.add_pane`` auto-activates a pane ONLY when the content was
   previously empty; a second arrival while a tab is already showing is
   added inert, in the background.
3. **Out-of-order answering + by-id delivery** — Left/Right lets the user
   pick ANY pending tab; the answer is delivered targeted at THAT
   intervention's id, never head-of-queue. (Migrates the P2 F1-interim
   test's property (b), the by-id witness.)
4. **Answered tabs stay** (✓-labelled, form disabled) until every pending
   intervention resolves, at which point the whole panel collapses.
5. **Pre-highlight (owner decision (A)) is unconditional** — the first
   option is pre-highlighted on the panel's initial show AND on every
   explicit tab switch alike (no re-route-vs-initial distinction anymore).
6. **Churn-zero** — resolving updates the SAME flow entry in place.
7. **Neutralization** — the tab label, the pane title, and the pane detail
   are THREE INDEPENDENT LLM-derived-text rendering surfaces, each stripped
   of raw ESC/OSC at its own call site.
8. **Left/Right switch tabs even with a RadioSet focused** — Textual's own
   ``RadioSet`` binds ``left``/``right`` as ``up``/``down`` aliases, so a
   naive ancestor binding could never fire while a RadioSet has focus; the
   panel uses a ``priority=True`` binding (Textual's priority pass runs
   outermost-ancestor-first, before the focused widget's own bindings) to
   win regardless.

Plus the surviving P1 gates: free-text reachability, no-double-display
(flow entry never renders chips), focus-return (Esc/Tab), the bracket-
decorated-label display-bug regression, and the pending-state dim gutter.

All use real instances (a concrete recording :class:`ClientTransport`, a real
mounted :class:`TextualChatApp`, real :class:`OutboxMessage`) — no mocks — per
the testing policy.
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, RadioButton, RadioSet, Static, Tab, TabbedContent, TabPane
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.intervention_choices import file_access_choices, generic_yn_choices
from reyn.runtime.outbox import OutboxMessage

_GUTTER_WIDTH = 2


class RecordingTransport(ClientTransportStub):
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
        # (choice_id | text, intervention_id) pairs — records the id an answer
        # was targeted at (#3299 P2 R1 by-id delivery), ``None`` when the
        # caller left it head-targeted.
        self.answered_choice_ids: "list[str | None]" = []
        self.answered_text_ids: "list[str | None]" = []

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

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        self.answered_text.append(text)
        self.answered_text_ids.append(intervention_id)
        return True

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        self.answered_choice.append(choice_id)
        self.answered_choice_ids.append(intervention_id)
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


def _second_choice_intervention() -> OutboxMessage:
    """A second, distinct closed-set intervention (different id) — used by the
    multi-pending tests to exercise TWO simultaneously-outstanding
    interventions, which ``outstanding_interventions`` supports by design."""
    return OutboxMessage(
        kind="intervention",
        text="Overwrite existing file?\n  Yes / No",
        meta={
            "intervention_id": "iv-2",
            "intervention_kind": "confirm",
            "prompt": "Overwrite existing file?",
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
            "nodes": [
                {"component": "text", "text": "Overwrite existing file?"},
                {"component": "list", "items": ["Yes", "No"]},
            ],
        },
    )


def _third_choice_intervention() -> OutboxMessage:
    """A THIRD, distinct closed-set intervention — used by the out-of-order
    (#3308 AC3) test to exercise three simultaneously-outstanding
    interventions."""
    return OutboxMessage(
        kind="intervention",
        text="Delete the branch?\n  Yes / No",
        meta={
            "intervention_id": "iv-3",
            "intervention_kind": "confirm",
            "prompt": "Delete the branch?",
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
            "nodes": [
                {"component": "text", "text": "Delete the branch?"},
                {"component": "list", "items": ["Yes", "No"]},
            ],
        },
    )


#: #5057 axis B: a resolved LIVE entry is folded to "intervention_resolved"
#: in place (the SAME entry object — churn-zero, #3299 P2 §4), never back to
#: plain "intervention". These two helpers look up "the intervention flow
#: entry/entries" both before AND after resolving, so both kinds count.
_IV_KINDS = ("intervention", "intervention_resolved")


def _iv_entry(app: TextualChatApp):
    entries = [
        e for e in app.query_one(FlowView).entries if e.item.kind in _IV_KINDS
    ]
    assert len(entries) == 1, f"expected one intervention entry, got {len(entries)}"
    return entries[0]


def _iv_entries(app: TextualChatApp):
    """All intervention flow entries, keyed by their ``intervention_id`` meta."""
    return {
        e.item.meta.get("intervention_id"): e
        for e in app.query_one(FlowView).entries
        if e.item.kind in _IV_KINDS
    }


def _tabs(panel: InterventionPanel) -> TabbedContent:
    return panel.query_one(TabbedContent)


def _active_pane(panel: InterventionPanel) -> TabPane:
    tabs = _tabs(panel)
    return tabs.get_pane(tabs.active)


def _pane_title(pane: TabPane) -> str:
    return pane.query_one(".iv-pane-title", Static).content.plain


def _tab_labels(panel: InterventionPanel) -> "list[str]":
    """Every tab-bar caption, in insertion order (public ``Tab.label_text``,
    not private state)."""
    return [tab.label_text for tab in panel.query(Tab)]


def _pane_ids_in_order(panel: InterventionPanel) -> "list[str]":
    """Every mounted pane's id, in insertion (DOM) order — from the public
    ``TabPane.id`` directly (``Tab.id`` is a DIFFERENT, internally-prefixed
    id namespace — ``--content-tab-<pane id>`` — not usable with
    ``TabbedContent.get_pane``), never from the panel's private
    ``_pane_ids`` lookup table."""
    return [pane.id for pane in panel.query(TabPane) if pane.id is not None]


async def _settle_until(pilot, until) -> None:
    """Pump until ``until()`` is true (#3748: unbounded, owner policy).

    Modelled on ``tests/interfaces/test_textual_chat_copy_rewind_3362.py``'s
    ``_settle``, and on #3651's rule: wait for a signal, never for a
    wall-clock guess. A hang here surfaces via CI's own kill-switch,
    naming this exact loop -- callers' own assertions still fail for the
    real reason if the wait ever resolves on a false signal.

    ``pilot.pause()`` runs BEFORE every check, unconditionally -- it is
    not just a delay, it is the pump that flushes pending UI messages, so
    an already-true predicate must still get one pass before returning
    (lead-coder review: checking first would silently skip that flush)."""
    while True:
        await pilot.pause()
        if until():
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_choice_intervention_panel_selection_delivers_correct_choice_id() -> None:
    """Tier 2b: F1 permission-band reachability — a closed-set intervention
    frame populates a tab's RadioSet; selecting the SECOND option delivers
    that option's ``choice_id`` ("no") through ``answer_intervention_choice``,
    and the flow entry resolves. Non-vacuous: the SECOND option is chosen (a
    first-option shortcut would fail) and the exact id is asserted; no answer
    is delivered before the selection."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is True, "panel did not show for a pending intervention"
        pane = _active_pane(panel)
        radio = pane.query_one(RadioSet)
        assert radio.has_focus, "panel did not auto-focus the tab's RadioSet"

        # No answer is delivered until the user acts.
        assert transport.answered_choice == []

        # #3299 P2 owner decision (A): the panel pre-highlights the FIRST
        # option on appear, so ONE "down" now reaches the SECOND option ("No").
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["no"], (
            f"choice not delivered; got {transport.answered_choice}"
        )
        assert transport.answered_choice_ids == ["iv-1"], (
            "answer not delivered BY ID (#3299 P2 R1)"
        )
        # Resolved reflection: DEFAULT gutter (#3299 P2 §5) + panel collapsed
        # (the only pending intervention resolved) + focus back to Composer.
        resolved = _iv_entry(app)
        assert resolved.state is EntryState.DEFAULT
        assert resolved.item.meta.get("_answer_label") == "No"
        assert panel.display is False, "panel did not collapse after resolving"
        assert app.query_one(Composer).has_focus, (
            "focus did not return to the Composer after resolving"
        )


@pytest.mark.asyncio
async def test_free_text_intervention_answered_via_panel_input() -> None:
    """Tier 2b: a free-text intervention (no choices) populates its tab's
    Input (auto-focused); submitting delivers the text through
    ``answer_intervention_text``."""
    transport = RecordingTransport([_free_text_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        pane = _active_pane(panel)
        text_input = pane.query_one(Input)
        assert text_input.has_focus, "panel did not auto-focus the tab's Input"

        await pilot.press("o", "k")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

    assert transport.answered_text == ["ok"]
    assert transport.answered_text_ids == ["iv-2"]
    assert transport.submitted == []
    assert transport.answered_choice == []


@pytest.mark.asyncio
async def test_composer_submit_during_pending_intervention_is_always_a_new_turn() -> None:
    """Tier 2b: no-double-input — the Composer no longer special-cases a
    pending intervention at all. A Composer submit while a closed-set
    intervention is pending is ALWAYS routed to ``submit_user_text`` — never
    ``answer_intervention_choice`` / ``answer_intervention_text`` — even when
    the text happens to name an option."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        # #4051: the arriving intervention frame's own hidden→shown transition
        # posts a TabbedContent.TabActivated message that schedules a DEFERRED
        # call_after_refresh(radios[0].focus) (intervention_panel.py's
        # on_tabbed_content_tab_activated) — a ONE-SHOT callback that can land
        # on any later refresh, including one after a plain composer.focus()
        # call, stealing focus back to the RadioSet with nothing left to
        # re-claim it: the keypresses below then land on the RadioSet instead
        # of the Composer, submit_user_text is never called, and
        # transport.submitted stays empty forever (the wait at the bottom of
        # this test is unbounded by design, #3748). A single focus() + wait-
        # for-condition does not structurally close this — the one-shot steal
        # can still land AFTER the check passes. RE-ASSERT focus every pump
        # until it demonstrably sticks, so the race resolves regardless of
        # which pump the deferred callback lands on (same "wait on the
        # condition, not a pause count" shape as #4044's fix, but the
        # condition here needs an accompanying retry, not just an observation).
        while app.focused is not composer:
            composer.focus()
            await pilot.pause()
        await pilot.press("y", "e", "s")
        await pilot.press("enter")
        # #3720: wait for the submit to ARRIVE, not for one turn of the loop.
        # A bare ``pause()`` asserts that the send completes within a single
        # pass of the event loop, which is a property of the machine rather
        # than of the code — it went red once on CI's 3.11 runner and green on
        # a rerun of the same commit. The predicate never weakens the
        # assertion: if the submit never lands, the assert below still fails,
        # just after waiting rather than before.
        await _settle_until(pilot, lambda: transport.submitted)

    assert transport.submitted == ["yes"]
    assert transport.answered_choice == []
    assert transport.answered_text == []


def test_chip_drawing_symbols_are_retired() -> None:
    """Tier 1: retire grep-zero (non-vacuity witness) — the presenter module no
    longer defines the chip-drawing surface at all."""
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
    option labels (which live exclusively in a tab's RadioSet now)."""
    from rich.console import Console

    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry = _iv_entry(app)
        presentation = await app._presenter.present(entry, 80)

    console = Console(width=80, no_color=True)
    with console.capture() as cap:
        console.print(presentation.renderable)
    rendered = cap.get()

    assert "Allow write to /etc/hosts?" in rendered
    assert "respond in the panel below" in rendered
    assert "[ 1 ·" not in rendered and "[ 2 ·" not in rendered, (
        "chip-shaped option markup leaked into the pending flow entry"
    )


@pytest.mark.asyncio
async def test_escape_from_panel_returns_focus_to_composer_tab_moves_forward() -> None:
    """Tier 2b: focus-return — Esc inside the panel returns focus to the
    Composer WITHOUT answering; the intervention stays pending (the panel
    stays open).

    #3365: the panel's own explicit "Tab -> back to composer" binding was
    REMOVED (architect ruling: Tab is forward-only everywhere in the app,
    Esc alone owns "back" — gated on
    test_textual_chat_esc_sufficiency_3365.py). Tab from the panel's RadioSet
    still happens to land on the Composer below via Textual's default
    forward focus-cycling (the Composer is simply next in the focus chain
    here) — asserted as a non-vacuity check that removing the binding didn't
    strand focus, NOT as evidence of an intentional "back" contract for Tab."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        panel = app.query_one(InterventionPanel)
        pane = _active_pane(panel)
        assert pane.query_one(RadioSet).has_focus

        await pilot.press("escape")
        await pilot.pause()

        assert app.query_one(Composer).has_focus, "Esc did not return focus to the Composer"
        assert panel.display is True, "Esc must not answer/collapse the panel"
        assert transport.answered_choice == []
        assert transport.answered_text == []

        # Re-focus the panel and confirm Tab's default forward-cycling still
        # lands somewhere sane (not stuck) — not asserting a "back" contract.
        _active_pane(panel).query_one(RadioSet).focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()

        assert not pane.query_one(RadioSet).has_focus, (
            "Tab did not move focus forward at all — focus is stuck on the panel"
        )
        assert panel.display is True
        assert transport.answered_choice == []


def test_choice_labels_are_neutralized_before_rendering() -> None:
    """Tier 2c: an LLM-derived choice LABEL carrying raw terminal control
    sequences must not leak into the flow entry's rendering.

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


@pytest.mark.asyncio
async def test_bracket_decorated_option_labels_render_intact() -> None:
    """Tier 1: a tab's RadioButton must expose the FULL literal option label —
    the real-TTY-witnessed display bug where the FIRST character of every
    option label was dropped ("Yes" → "es", "No" → "o", etc).

    NON-VACUITY (falsification): on the pre-fix code (``RadioButton(label)``
    with a bare ``str``) this assertion FAILS — every label above renders with
    its first character (and the ``[x]`` bracket) missing. Verified locally by
    reverting the ``Content(label)`` fix in ``InterventionPanel.add_pending``.
    """

    class _PanelHost(App):
        def compose(self) -> ComposeResult:
            yield InterventionPanel(id="panel")

    yn_choices = generic_yn_choices()
    long_choices = file_access_choices("/tmp/project")

    app = _PanelHost()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)

        panel.add_pending(
            "k1",
            prompt="Proceed?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey} for c in yn_choices
            ],
        )
        await pilot.pause()
        radio = _active_pane(panel).query_one(RadioSet)
        rendered_yn = [rb.label.plain for rb in radio.query(RadioButton)]
        assert rendered_yn == ["[y]es", "[A]lways", "[n]o", "[N]ever"], (
            f"bracket-decorated label(s) dropped a character; got {rendered_yn!r}"
        )

        panel.add_pending(
            "k2",
            prompt="Grant file access?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey} for c in long_choices
            ],
        )
        await pilot.pause()
        # k2 arrives while k1 is already showing — it does NOT become active
        # (#3308 AC2), so query its pane directly by id rather than "active".
        pane2 = _tabs(panel).get_pane(_pane_ids_in_order(panel)[1])
        radio2 = pane2.query_one(RadioSet)
        rendered_long = [rb.label.plain for rb in radio2.query(RadioButton)]
        assert rendered_long == [
            "[y]es",
            "[j]ust this path always",
            "[r]ecursive under '/tmp/project' always",
            "[N]o",
        ], f"long/bracket-decorated label(s) dropped a character; got {rendered_long!r}"


# --- panel neutralize-guard witnesses (3 independently-witnessed surfaces) --
# #3308: the tab-ified panel has THREE separate LLM-derived-text rendering
# surfaces — the tab-bar LABEL, the pane TITLE, and the pane DETAIL — each
# neutralized at its OWN call site in ``InterventionPanel.add_pending`` (see
# that method's docstring). Each test below is scoped to assert ONLY the ESC
# byte's absence, so each is a genuine, independent witness of ITS site.
_ESC_OSC_PAYLOAD = "\x1b[31mRED\x1b]0;pwn\x07"


class _PanelOnlyApp(App):
    def compose(self) -> ComposeResult:
        yield InterventionPanel(id="panel")


@pytest.mark.asyncio
async def test_panel_choice_label_neutralizes_raw_esc_osc() -> None:
    """Tier 2c: the tab's RadioButton LABEL surface is independently
    neutralized.

    NON-VACUITY (falsification, verified locally): reverting ONLY the
    ``_neutralized_label`` call around the label in
    ``InterventionPanel.add_pending`` makes this assertion FAIL — ``Content``'s
    own control-code stripping does not remove ESC (0x1B). Reverting the
    tab-label/title/detail neutralize (the other sites) does NOT affect this
    assertion — the label survives its own site's guard alone."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.add_pending(
            "k",
            prompt="Proceed?",
            detail=None,
            choices=[{"id": "x", "label": _ESC_OSC_PAYLOAD, "hotkey": "x"}],
        )
        await pilot.pause()
        radio = _active_pane(panel).query_one(RadioSet)
        (only_button,) = radio.query(RadioButton)
        rendered_label = only_button.label.plain
        assert "\x1b" not in rendered_label, (
            f"raw ESC leaked into the panel's RadioButton label: {rendered_label!r}"
        )
        assert "RED" in rendered_label


@pytest.mark.asyncio
async def test_panel_tab_label_neutralizes_raw_esc_osc() -> None:
    """Tier 2c: the tab-BAR CAPTION surface is independently neutralized —
    NEW in #3308 (P1/P2 had no tab bar at all).

    NON-VACUITY (falsification, verified locally): reverting ONLY the
    ``tab_label_text = _neutralized_label(prompt)`` call in
    ``InterventionPanel.add_pending`` (passing the raw ``prompt`` to
    ``_tab_label`` instead) makes this assertion FAIL. Reverting the title or
    detail neutralize (the other two sites) does NOT affect this assertion."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.add_pending("k", prompt=_ESC_OSC_PAYLOAD, detail=None, choices=None)
        await pilot.pause()
        (label,) = _tab_labels(panel)
        assert "\x1b" not in label, f"raw ESC leaked into the tab label: {label!r}"
        assert "RED" in label


@pytest.mark.asyncio
async def test_panel_title_neutralizes_raw_esc_osc() -> None:
    """Tier 2c: the pane TITLE surface is independently neutralized.

    NON-VACUITY (falsification, verified locally): reverting ONLY the
    ``title_text = _neutralized_label(prompt)`` call in
    ``InterventionPanel.add_pending`` (passing ``Content(prompt)`` directly)
    makes this assertion FAIL. Reverting the tab-label or detail neutralize
    (the other two sites) does NOT affect this assertion."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.add_pending("k", prompt=_ESC_OSC_PAYLOAD, detail=None, choices=None)
        await pilot.pause()
        rendered = _pane_title(_active_pane(panel))
        assert "\x1b" not in rendered, (
            f"raw ESC leaked into the panel's title: {rendered!r}"
        )
        assert "RED" in rendered


@pytest.mark.asyncio
async def test_panel_detail_neutralizes_raw_esc_osc() -> None:
    """Tier 2c: the pane DETAIL surface is independently neutralized.

    NON-VACUITY (falsification, verified locally): reverting ONLY the
    ``detail_text = _neutralized_label(detail)`` call in
    ``InterventionPanel.add_pending`` (passing ``Content(detail)`` directly)
    makes this assertion FAIL. Reverting the tab-label or title neutralize
    (the other two sites) does NOT affect this assertion."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.add_pending(
            "k", prompt="Proceed?", detail=_ESC_OSC_PAYLOAD, choices=None
        )
        await pilot.pause()
        detail = _active_pane(panel).query_one(".iv-pane-detail", Static)
        rendered = detail.content.plain
        assert "\x1b" not in rendered, (
            f"raw ESC leaked into the panel's detail: {rendered!r}"
        )
        assert "RED" in rendered


# --- #3308 (#3299 P5): tab-ify — one tab per pending, no re-route -----------


@pytest.mark.asyncio
async def test_second_enter_after_answering_does_not_deliver_to_unread_second() -> None:
    """Tier 2b: ★AC1 — with TWO interventions pending, answering the FIRST
    (bare Enter, pre-highlighted "Yes") does NOT move the active tab, so a
    muscle-memory SECOND bare ``Enter`` lands on the SAME (now ✓-answered,
    disabled) tab and delivers NOTHING to the still-unread second
    intervention. Migrates the retired P2 F1-interim test's safety property
    (a) onto the tab-ified structure (#3308 co-vet correction: the interim
    MECHANISM is retired, this PROPERTY is not).

    NON-VACUITY (falsification, verified locally): the disable loop itself is
    independently witnessed by
    ``test_answered_tab_stays_visible_until_all_resolve``'s focus-refusal
    probe, not by THIS test's keyboard replay — after answering,
    ``mark_answered`` also moves focus onto the panel's ``Tabs`` bar
    (:meth:`InterventionPanel.mark_answered`'s docstring), so a keyboard-only
    Down+Enter probe here lands on the Tabs bar, not the (still technically
    reachable-by-direct-``.focus()``) RadioSet — stripping ``disabled = True``
    alone does NOT flip this specific test RED (confirmed by trial: the focus
    re-anchor already blocks the keyboard path on its own). What THIS test
    demonstrates instead is the observable end-to-end behavior a user
    actually experiences: neither a repeat bare ``Enter`` (RadioSet doesn't
    re-post ``Changed`` for re-toggling an already-selected button — true
    regardless of ``disabled``) nor Down+Enter re-delivers after resolving."""
    transport = RecordingTransport(
        [_choice_intervention(), _second_choice_intervention()], end=False
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        await pilot.press("enter")  # answers iv-1 ("Yes", pre-highlighted)
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["yes"]
        assert transport.answered_choice_ids == ["iv-1"]

        # Muscle-memory second bare Enter — must be a no-op.
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["yes"], (
            "a second bare Enter delivered an unrequested answer — "
            f"got {transport.answered_choice}"
        )
        entries = _iv_entries(app)
        assert entries["iv-2"].item.meta.get("_answer_label") is None, (
            "the second (still-pending) intervention must remain unanswered"
        )

        # Stronger probe: navigate WITHIN the answered tab (Down then Enter,
        # picking a DIFFERENT option) — this is what the disabled form must
        # actually block (the bare-Enter-alone case above is a no-op even
        # without disabling, since RadioSet itself doesn't re-fire ``Changed``
        # for re-toggling the SAME already-selected button).
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["yes"], (
            "navigating within the answered (disabled) tab still delivered — "
            f"got {transport.answered_choice}"
        )


@pytest.mark.asyncio
async def test_new_pending_intervention_does_not_steal_the_active_tab() -> None:
    """Tier 2b: ★AC2 — with the panel already showing the FIRST intervention,
    a SECOND arriving does not move the active tab; the first stays active
    and answerable by a bare Enter. Migrates the retired P2 F1-interim test's
    coverage of "the second intervention's entry must not be touched".

    NON-VACUITY (falsification, verified locally): temporarily adding
    ``self.call_after_refresh(lambda: setattr(tabs, "active", pane_id))``
    at the end of ``InterventionPanel.add_pending`` (force-stealing the
    active tab on every arrival, deferred past the new pane's own mount so
    the force actually takes effect) flips the assertion below RED — the
    active pane becomes the SECOND ("Overwrite existing file?") instead of
    staying on the first ("...hosts?")."""
    transport = RecordingTransport(
        [_choice_intervention(), _second_choice_intervention()], end=False
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        entries = _iv_entries(app)
        assert set(entries) == {"iv-1", "iv-2"}, (
            f"expected both pending interventions to get their own flow entry, got {set(entries)}"
        )
        panel = app.query_one(InterventionPanel)
        assert "hosts" in _pane_title(_active_pane(panel)), (
            "a new arrival stole the active tab from the first pending intervention"
        )

        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice_ids == ["iv-1"], (
            f"bare Enter did not answer the still-active FIRST tab; got {transport.answered_choice_ids}"
        )
        entries = _iv_entries(app)
        assert entries["iv-2"].item.meta.get("_answer_label") is None


@pytest.mark.asyncio
async def test_out_of_order_answer_targets_the_selected_tab_by_id() -> None:
    """Tier 2b: ★AC3 — with THREE interventions pending, Left/Right selects
    the THIRD tab directly and answers it; the other two stay untouched.
    Migrates the retired P2 F1-interim test's by-id witness (b) onto
    out-of-order selection instead of FIFO re-route.

    NON-VACUITY (falsification, verified locally): reverting
    ``on_intervention_panel_choice_selected``'s ``intervention_id=iv_id`` to
    ``intervention_id=None`` flips ``transport.answered_choice_ids`` to
    ``[None]`` instead of ``["iv-3"]``."""
    transport = RecordingTransport(
        [
            _choice_intervention(),
            _second_choice_intervention(),
            _third_choice_intervention(),
        ],
        end=False,
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        assert set(_iv_entries(app)) == {"iv-1", "iv-2", "iv-3"}

        await pilot.press("right")
        await pilot.press("right")
        await pilot.pause()
        panel = app.query_one(InterventionPanel)
        assert "branch" in _pane_title(_active_pane(panel)), (
            "Left/Right did not reach the third pending intervention's tab"
        )

        await pilot.press("enter")  # pre-highlighted "Yes" on the THIRD tab
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice_ids == ["iv-3"], (
            f"answer not targeted at the selected THIRD intervention; got {transport.answered_choice_ids}"
        )
        entries = _iv_entries(app)
        assert entries["iv-3"].item.meta.get("_answer_label") == "Yes"
        assert entries["iv-1"].item.meta.get("_answer_label") is None
        assert entries["iv-2"].item.meta.get("_answer_label") is None


@pytest.mark.asyncio
async def test_answered_tab_stays_visible_until_all_resolve() -> None:
    """Tier 2b: ★AC4 — an answered tab is ✓-labelled and its form disabled,
    but it STAYS mounted (never removed); the panel itself collapses only
    once every pending intervention has resolved.

    NON-VACUITY (falsification): if ``mark_answered`` removed the pane
    (``tabs.remove_pane``) instead of disabling it in place, the ✓-labelled
    "hosts" tab would be GONE from the tab bar entirely after answering it.
    Separately, if the ``control.disabled = True`` loop were removed, the
    ``.disabled is True`` assertion below fails directly, and the focus-
    refusal probe (a disabled widget's ``.focus()`` is a no-op, verified
    against the installed Textual 8.2.8) would instead succeed. Both
    verified locally by trial-reverting each independently."""
    transport = RecordingTransport(
        [_choice_intervention(), _second_choice_intervention()], end=False
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        panel = app.query_one(InterventionPanel)

        await pilot.press("enter")  # answers iv-1
        await pilot.pause()
        await pilot.pause()

        labels = _tab_labels(panel)
        assert any(label.startswith("✓") and "hosts" in label for label in labels), (
            f"answered tab was removed instead of staying ✓-labelled; got {labels!r}"
        )
        assert any("Overwrite" in label and not label.startswith("✓") for label in labels), (
            f"the still-pending second tab is missing; got {labels!r}"
        )
        first_pane = _tabs(panel).get_pane(_pane_ids_in_order(panel)[0])
        first_radio = first_pane.query_one(RadioSet)
        assert first_radio.disabled is True, "answered tab's form was not disabled"
        # A disabled widget REFUSES focus outright (verified against the
        # installed Textual 8.2.8: ``Widget.focus()`` on a ``disabled``
        # widget is a no-op) — the strongest available falsifiable probe
        # that the answered form is genuinely inert, independent of
        # whatever keyboard path might otherwise reach it.
        first_radio.focus()
        await pilot.pause()
        assert app.focused is not first_radio, (
            "the answered tab's disabled RadioSet accepted focus — not inert"
        )
        assert panel.display is True, "panel collapsed with a pending intervention still unanswered"

        await pilot.press("right")
        await pilot.press("enter")  # answers iv-2, the LAST pending one
        await pilot.pause()
        await pilot.pause()

        assert panel.display is False, "panel did not collapse once every intervention resolved"
        assert app.query_one(Composer).has_focus


@pytest.mark.asyncio
async def test_prehighlight_uniform_on_initial_show_and_tab_switch() -> None:
    """Tier 2b: ★AC5 — the first option is pre-highlighted BOTH on the
    panel's initial show (bare Enter answers "Yes" on iv-1) AND after
    switching to a fresh tab (bare Enter, no Down first, answers "Yes" on
    iv-2 too) — owner decision (A), now unconditional (#3308 retires the P2
    ``initial``/re-route distinction entirely: the active tab never moves
    except by explicit navigation, so there is no "unread re-route" case left
    to guard against).

    NON-VACUITY (falsification, verified locally): removing the
    focus-follows-activation body of
    ``InterventionPanel.on_tabbed_content_tab_activated`` (never focusing the
    newly-active pane's RadioSet/Input) flips even the FIRST assertion below
    RED — a bare Enter delivers nothing at all once focus never lands on any
    form."""
    transport = RecordingTransport(
        [_choice_intervention(), _second_choice_intervention()], end=False
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        await pilot.press("enter")  # initial show pre-highlight
        await pilot.pause()
        await pilot.pause()
        assert transport.answered_choice == ["yes"]

        await pilot.press("right")  # switch to the (still pending) iv-2 tab
        await pilot.pause()

        await pilot.press("enter")  # tab-switch pre-highlight, no Down first
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["yes", "yes"], (
            f"bare Enter after a tab switch did not answer the pre-highlighted "
            f"first option; got {transport.answered_choice}"
        )
        assert transport.answered_choice_ids == ["iv-1", "iv-2"]


@pytest.mark.asyncio
async def test_left_right_switch_tabs_even_with_radioset_focused() -> None:
    """Tier 1: ★AC8 — Left/Right switch the ACTIVE TAB even while a
    ``RadioSet`` has focus, via a ``priority=True`` binding on the panel
    (Textual's priority pass runs BEFORE the focused-widget-outward walk that
    would otherwise let ``RadioSet``'s own ``left``/``right`` = prev/next-
    option aliases win).

    NON-VACUITY (falsification, verified locally): removing ``priority=True``
    from the panel's ``left``/``right`` ``Binding`` entries makes the active
    tab stay UNCHANGED after ``Right`` — the key is instead consumed by the
    focused ``RadioSet``'s own ``next_button`` action (moving its highlight,
    not the tab) — flipping the assertion below RED."""
    transport = RecordingTransport(
        [_choice_intervention(), _second_choice_intervention()], end=False
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        panel = app.query_one(InterventionPanel)
        tabs = _tabs(panel)
        active_before = tabs.active
        radio = _active_pane(panel).query_one(RadioSet)
        assert radio.has_focus

        await pilot.press("right")
        await pilot.pause()

        assert tabs.active != active_before, (
            "Right did not switch the active tab while a RadioSet had focus"
        )
        assert "Overwrite" in _pane_title(_active_pane(panel))
        # The FIRST tab's own RadioSet selection must be untouched by the
        # Right keypress (it must have been consumed as a TAB switch, not a
        # RadioSet next-option action).
        first_pane = tabs.get_pane(_pane_ids_in_order(panel)[0])
        assert first_pane.query_one(RadioSet).pressed_index == -1, (
            "Right moved the RadioSet's own highlight instead of switching tabs"
        )


# --- #3299 P2 §5: pending EntryState is DEFAULT, never RUNNING/SUCCESS/ERROR


@pytest.mark.asyncio
async def test_pending_intervention_entry_state_is_default_with_dim_awaiting_glyph() -> None:
    """Tier 2b: a PENDING intervention's flow entry stays ``EntryState.DEFAULT``
    (never ``RUNNING`` — would wrongly trip the #72 orphan-sweep + the ②
    live-spinner, an intervention is not a tool; never ``SUCCESS``/``ERROR`` —
    they imply an outcome, the #3296 don't-fabricate-a-classification lesson).
    The gutter distinguishes "awaiting" from an ordinary DEFAULT row with a
    dim kind-driven glyph instead of the state color.

    NON-VACUITY (falsification): reverting the ``kind == "intervention"``
    branch in ``gutter._gutter_glyph_color`` makes the pending glyph fall
    through to the ordinary (non-dim, "◆ needs you") intervention glyph
    regardless of pending/resolved — this assertion (dim colour while
    pending) would then fail, verified locally."""
    from textual_flowview import EntryState as _EntryState

    from reyn.interfaces.inline.textual_chat.gutter import ReynGutter
    from reyn.interfaces.repl.renderer import _CC_DIM

    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        entry = _iv_entry(app)
        assert entry.state is _EntryState.DEFAULT
        assert entry.state not in (
            _EntryState.RUNNING,
            _EntryState.SUCCESS,
            _EntryState.ERROR,
        )
        gutter = ReynGutter()
        rendered = gutter.decorate(entry, width=2, height=1)
        assert rendered.style == _CC_DIM, (
            f"pending intervention gutter glyph is not dim; style={rendered.style!r}"
        )


# --- #3324: a RESOLVED intervention's gutter must not read as "needs you" --
# Owner-reported: after answering, the flow entry's gutter stayed the same
# amber (_CC_WARN) as a still-pending one, because a resolved intervention
# stays EntryState.DEFAULT (#3299 P2 §5) and DEFAULT falls back to the
# entry's KIND colour — which for kind="intervention" IS that amber. Fixed
# in ``gutter._gutter_glyph_color`` (the "intervention" branch now special-
# cases the resolved leg too, returning ``_CC_DONE`` instead of falling
# through to ``_KIND_LINE``'s amber).


@pytest.mark.asyncio
async def test_resolved_intervention_gutter_is_not_the_needs_you_amber() -> None:
    """Tier 2b: a RESOLVED intervention's gutter colour must differ from the
    "needs you" amber (``_CC_WARN``) — that colour must mean "still pending",
    never "already answered".

    NON-VACUITY (falsification): reverting ``gutter._gutter_glyph_color``'s
    "intervention" branch to only special-case the PENDING leg (letting the
    resolved leg fall through to ``_KIND_LINE["intervention"]``) makes this
    assertion fail — verified locally: style becomes ``_CC_WARN`` again."""
    from reyn.interfaces.inline.textual_chat.gutter import ReynGutter
    from reyn.interfaces.repl.renderer import _CC_WARN

    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        await pilot.press("enter")  # answers the pre-highlighted "Yes"
        await pilot.pause()
        await pilot.pause()

        entry = _iv_entry(app)
        assert entry.state is EntryState.DEFAULT, (
            "resolving must not leave EntryState.DEFAULT (#3299 P2 §5 — "
            "an answer is not a SUCCESS/ERROR outcome)"
        )
        assert entry.item.meta.get("_answer_label") == "Yes"

        gutter = ReynGutter()
        rendered = gutter.decorate(entry, width=_GUTTER_WIDTH, height=1)
        assert rendered.style != _CC_WARN, (
            f"resolved intervention gutter still reads amber ('needs you'); "
            f"style={rendered.style!r}"
        )


@pytest.mark.asyncio
async def test_pending_resolved_and_ordinary_default_gutters_are_mutually_distinguishable() -> (
    None
):
    """Tier 2b: pending (dim ``⋯``), resolved (``_CC_DONE`` ``◆``), and an
    ordinary DEFAULT row (a plain user message, its own kind colour) render
    THREE mutually distinct (glyph, colour) pairs — none collapses onto
    another. Asserts on the actual rendered :class:`~rich.text.Text`
    (glyph + style), not on a constant lookup (verification-hazards §10/11).

    NON-VACUITY: with the pre-fix code (resolved intervention falling
    through to the amber kind colour), ``resolved_render`` would equal a
    ``(_CC_WARN, "◆")`` pair identical to what a still-pending, non-dim
    intervention row would show — this test's core assertion is exactly the
    inequality that regresses without the fix."""
    from reyn.interfaces.inline.textual_chat.gutter import ReynGutter

    transport = RecordingTransport(
        [OutboxMessage(kind="agent", text="hello"), _choice_intervention()], end=False
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        gutter = ReynGutter()
        flow = app.query_one(FlowView)
        agent_entry = next(e for e in flow.entries if e.item.kind == "agent")
        assert agent_entry.state is EntryState.DEFAULT

        pending_entry = _iv_entry(app)
        assert pending_entry.state is EntryState.DEFAULT

        agent_render = gutter.decorate(agent_entry, width=_GUTTER_WIDTH, height=1)
        pending_render = gutter.decorate(pending_entry, width=_GUTTER_WIDTH, height=1)

        await pilot.press("enter")  # answers the pre-highlighted "Yes"
        await pilot.pause()
        await pilot.pause()

        resolved_entry = _iv_entry(app)
        assert resolved_entry.item.meta.get("_answer_label") == "Yes"
        resolved_render = gutter.decorate(resolved_entry, width=_GUTTER_WIDTH, height=1)

        agent_pair = (agent_render.style, agent_render.plain.strip())
        pending_pair = (pending_render.style, pending_render.plain.strip())
        resolved_pair = (resolved_render.style, resolved_render.plain.strip())

        assert agent_pair != pending_pair, (
            "an ordinary DEFAULT row (agent) renders identically to a PENDING "
            f"intervention: {agent_pair!r}"
        )
        assert agent_pair != resolved_pair, (
            "an ordinary DEFAULT row (agent) renders identically to a RESOLVED "
            f"intervention: {agent_pair!r}"
        )
        assert pending_pair != resolved_pair, (
            "a PENDING intervention renders identically to a RESOLVED one — "
            f"resolving is not visually distinguishable: {pending_pair!r}"
        )


# --- #3299 P2 §4: placeholder→resolved is the SAME entry, churn-zero --------


@pytest.mark.asyncio
async def test_resolve_updates_the_same_entry_no_new_entry_appended() -> None:
    """Tier 2b: ★AC6 — non-vacuity witness for the churn-zero contract —
    resolving a pending intervention updates the SAME flow entry object in
    place; the SET of intervention entry objects is unchanged (identity-
    preserved) and the content becomes the Q→A record — never a second,
    additional entry.

    NON-VACUITY (falsification): if ``_resolve_intervention`` APPENDED a new
    entry instead of ``entry.set_item(...)``-ing the tracked one, the
    identity-set below would gain a SECOND, different entry object rather
    than staying exactly the one entry seen before resolving — this assertion
    would fail."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        before = {
            id(e) for e in app.query_one(FlowView).entries if e.item.kind in _IV_KINDS
        }
        entry_before = next(
            e for e in app.query_one(FlowView).entries if e.item.kind in _IV_KINDS
        )

        await pilot.press("enter")  # blind Enter answers the pre-highlighted "Yes"
        await pilot.pause()
        await pilot.pause()

        after = {
            id(e) for e in app.query_one(FlowView).entries if e.item.kind in _IV_KINDS
        }
        assert after == before, (
            "resolving changed the SET of intervention flow-entry objects — "
            "churn regressed (a new entry was appended instead of updating in place)"
        )
        assert entry_before.item.meta.get("_answer_label") == "Yes", (
            "the SAME entry object was not updated in place with the resolved answer"
        )


# --- #3311 tui-coder real-TTY finding: panel must not swallow the screen ----
# tui-coder's real-TTY witness found that ``InterventionPanel``'s OWN
# ``DEFAULT_CSS`` carried an ``InterventionPanel Tabs { height: auto; }``
# rule that overrode Textual's ``Tabs`` widget's own sensible fixed
# ``height: 2`` default (``textual.widgets.Tabs.DEFAULT_CSS``, verified
# against the installed Textual 8.2.8) — ``height: auto`` on ``Tabs`` (a
# widget that is NOT designed for auto-sizing) resolved to a hugely inflated
# value, and the panel's own ``height: auto`` then grew to match, pushing the
# FlowView and Composer off-screen. EVERY widget-state assertion in this file
# (``panel.display is True``, ``radio.has_focus``, tab labels, etc.) stayed
# green throughout — none of them look at LAYOUT GEOMETRY, so this defect
# slipped through all of them. These tests close that gap by asserting on
# ``Widget.region`` (Textual's own computed screen-space rectangle) directly.


@pytest.mark.asyncio
@pytest.mark.parametrize("screen_size", [(80, 24), (100, 60)])
async def test_pending_intervention_panel_does_not_swallow_the_screen(
    screen_size: "tuple[int, int]",
) -> None:
    """Tier 2b: ★ real-TTY-witnessed regression guard (#3311) — with ONE
    intervention pending, on TWO screen sizes: the panel's region must be a
    SMALL FRACTION of the screen height (not the whole screen), and the
    FlowView (conversation) + Composer (input row) must both be FULLY
    CONTAINED on-screen (``0 <= y`` AND ``y + height <= screen_height`` —
    BOTH bounds) with the FlowView NOT squashed to a hairline sliver.

    ★co-vet correction (an earlier version of this test asserted only
    ``region.height > 0`` and an upper-bound-only containment check
    (``y + height <= screen_height``) — BOTH are insufficient: measured
    directly against the actual pre-fix defect, the FlowView's region was
    ``y=-8, height=1`` — height IS non-zero (1), and ``y + height = -7 <=
    screen_height`` is trivially TRUE for a NEGATIVE ``y`` (pushed off the
    TOP of the screen), so neither check alone would have caught it. The
    Composer's defective region (``y=24, height=1`` on an 80x24 screen) WAS
    caught by an upper-bound check (``24 + 1 = 25 > 24``), but relying on
    that asymmetry across the two widgets is exactly the born-vacuous trap —
    this version requires the LOWER bound (``y >= 0``) explicitly for both,
    plus a not-squashed floor on the FlowView, so it cannot pass by
    coincidence of which widget happened to be pushed which direction.

    NON-VACUITY (falsification, verified locally against the actual pre-fix
    ``InterventionPanel Tabs { height: auto; }`` rule, both screen sizes):
    - 80x24: FlowView measured ``Region(x=0, y=-8, width=78, height=1)``
      (``y >= 0`` fails) and Composer ``Region(x=2, y=24, width=76,
      height=1)`` (``y + height = 25 > 24`` fails).
    - 100x60: FlowView ``Region(x=0, y=-8, width=98, height=1)`` (``y >= 0``
      fails), Composer ``Region(x=2, y=60, width=96, height=1)`` (``y +
      height = 61 > 60`` fails).
    A widget-state-only assertion (``panel.display is True``) would NOT have
    caught any of this — the panel WAS displayed, just enormous."""
    transport = RecordingTransport([_choice_intervention()], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=screen_size) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        flow = app.query_one(FlowView)
        composer = app.query_one(Composer)

        screen_height = app.size.height
        assert panel.region.height < screen_height // 2, (
            f"the pending-intervention panel's region ({panel.region!r}) "
            f"consumes more than half the {screen_height}-row screen"
        )
        for name, widget in (("FlowView", flow), ("Composer", composer)):
            region = widget.region
            assert region.y >= 0, (
                f"{name}'s region is pushed OFF the top of the screen "
                f"(negative y); region={region!r}"
            )
            assert region.y + region.height <= screen_height, (
                f"{name}'s region extends past the bottom of the "
                f"{screen_height}-row screen; region={region!r}"
            )
        # The FlowView must not be merely "contained" but squashed to a
        # hairline (the actual pre-fix defect measured height=1 while ALSO
        # being off-screen — a not-squashed floor closes the case where a
        # future regression keeps it on-screen but still crushes it).
        assert flow.region.height >= 3, (
            f"the FlowView (conversation) is squashed to a hairline while an "
            f"intervention is pending; region={flow.region!r}"
        )


def test_tabs_bar_has_no_height_override_reyn_relies_on_textuals_default() -> None:
    """Tier 1: reyn's contract for the tab-caption bar is NOT setting a height
    rule for it — not "Textual's Tabs defaults to height 2", which is
    Textual's own promise, not reyn's (#3311's real-TTY regression made this
    distinction concrete). An earlier revision added ``InterventionPanel
    Tabs { height: auto; }`` by (wrong) analogy with the ``TabbedContent``
    rule beside it; ``Tabs`` is NOT designed for auto-sizing and the override
    resolved to ~30 rows on an 80x24 screen (tui-coder's real-TTY witness),
    ballooning the whole panel and pushing the FlowView/Composer off-screen.
    Every widget-STATE assertion (displayed, focused) stayed green through
    that regression — only the actual CSS declaration catches it.

    Static (no real TTY, no Textual app, no ``pytest.mark.asyncio``): parses
    ``InterventionPanel.DEFAULT_CSS`` directly, so it runs even where
    the ``effects``/real-terminal extras are unavailable. Asserts no rule
    targets the bare ``Tabs`` type — word-bounded so ``TabbedContent`` (a
    different, legitimately-ruled type two lines below) never matches.

    A ``height`` rule reappearing on ``Tabs``, at ANY value — not just
    ``auto`` — is what this guards against: reyn's contract is "we don't
    touch it", not "Textual's default happens to be small". If Textual ever
    changes that default, this stays green (reyn still touches nothing);
    if reyn re-adds a rule, this goes red regardless of the value chosen.
    """
    css = InterventionPanel.DEFAULT_CSS
    tabs_rule = re.search(r"\bTabs\s*\{", css)
    assert tabs_rule is None, (
        "InterventionPanel.DEFAULT_CSS declares a rule for the bare `Tabs` "
        "selector — reyn's contract is to declare NONE (see #3311): adding "
        "one here, even a seemingly-harmless height:auto by analogy with "
        "the TabbedContent rule beside it, reproduces a real-TTY regression "
        "where Tabs (not designed for auto-sizing) ballooned to ~30 rows on "
        "an 80x24 screen and pushed the FlowView/Composer off-screen. "
        f"Found: {css[tabs_rule.start():tabs_rule.start() + 80]!r}"
    )
