"""Tier 2: #4751 — the intervention panel's displayed hotkey labels
(``[y]``/``[j]``/``[r]``/``[N]`` etc.) are wired to a real keypress.

Before this PR, ``grep hotkey`` under ``interfaces/inline/textual_chat/``
returned zero hits (the issue's own finding) — ``InterventionChoice.hotkey``
rode the wire all the way to this panel's own ``choices`` argument (every
producer already stamps it, and ``add_pending`` already received it), but
the panel never READ the field: each option became a bare
``RadioButton(Content(label))`` whose bracketed letter was decorative text
only. Pressing it while the panel held focus was silently swallowed by
``RadioSet`` (no bound action for a bare letter) — never reaching the
Composer either (a different, mutually exclusive focus state, per
``test_composer_submit_during_pending_intervention_is_always_a_new_turn``'s
own pinned scenario).

Owner ruling (issue #4751 thread, superseding the earlier A/B framing):
"表示どおりに配線する" — wire every displayed hotkey. Accepted per the
issue's own explicit acceptance criteria: a hotkey **selects** the matching
option (arrow+Enter unchanged — Enter still confirms); an UNDISPLAYED
character is left alone (deny side — this must not become "consume every
keypress").

Case-sensitivity is load-bearing, not cosmetic: ``generic_yn_choices()``
puts ``n`` (one-shot deny) and ``N`` (deny + persist to
``.reyn/approvals.yaml``) in the SAME set — ``user_intervention.match_choice``
already treats them as two distinct, case-sensitive choices for the
REPL/stdin path, so this panel must not blur that distinction.

Real ``InterventionPanel`` mounted directly (mirrors
``test_bracket_decorated_option_labels_render_intact`` in
``test_textual_chat_intervention_panel_3299.py``, the sibling test this
file's fixtures/helpers are copied from) — no mocks, per the testing
policy.
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import RadioButton, RadioSet, TabbedContent

from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.intervention_choices import file_access_choices, generic_yn_choices


class _PanelOnlyApp(App):
    def compose(self) -> ComposeResult:
        yield InterventionPanel(id="panel")


class _RecordingPanelApp(_PanelOnlyApp):
    """Records every ``ChoiceSelected`` the panel posts — the PUBLIC
    delivery signal (mirrors ``RecordingTransport.answered_choice`` in the
    sibling #3299 test file, which normally sits between this message and
    the wire; this file has no transport, so the message itself is the
    public surface a hotkey-press-alone test can assert against without
    reaching into the panel's own private ``_answered`` set)."""

    def __init__(self) -> None:
        super().__init__()
        self.selected: "list[str]" = []

    def on_intervention_panel_choice_selected(
        self, event: InterventionPanel.ChoiceSelected
    ) -> None:
        self.selected.append(event.choice_id)


def _active_pane(panel: InterventionPanel):
    tabs = panel.query_one(TabbedContent)
    return tabs.get_pane(tabs.active)


def _selected_label(panel: InterventionPanel) -> str:
    radio = _active_pane(panel).query_one(RadioSet)
    assert radio._selected is not None, "nothing selected"
    return list(radio.query(RadioButton))[radio._selected].label.plain


@pytest.mark.asyncio
async def test_pressing_a_displayed_hotkey_selects_but_does_not_confirm():
    """Tier 2: the core witness — pressing ``n`` (displayed as ``[n]o``)
    moves the RadioSet's selection to that option WITHOUT delivering an
    answer; a subsequent ``Enter`` is still required (B-shaped: select
    only, arrow+Enter unchanged)."""
    app = _RecordingPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.add_pending(
            "k1",
            prompt="Proceed?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in generic_yn_choices()
            ],
        )
        await pilot.pause()
        radio = _active_pane(panel).query_one(RadioSet)
        assert radio.has_focus
        assert _selected_label(panel) == "[y]es", "pre-highlight must start on the first option"

        await pilot.press("n")
        await pilot.pause()

        assert _selected_label(panel) == "[n]o", (
            "the 'n' hotkey must select the '[n]o' option, not merely be ignored"
        )
        # Falsify-adjacent: selecting must not itself have answered anything —
        # RadioSet.Changed only fires on toggle (Enter/Space), never on a bare
        # selection move (`RadioSet.action_next_button`'s own shape, which
        # this handler mirrors) — asserted via the PUBLIC `ChoiceSelected`
        # message (`_RecordingPanelApp`), not the panel's private state.
        assert app.selected == [], "a hotkey press alone must not deliver an answer"

        # Enter still confirms — arrow+Enter's own contract is unchanged.
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected == ["no"], "Enter must still confirm the hotkey-selected option"


@pytest.mark.asyncio
async def test_hotkey_is_case_sensitive_n_and_shift_n_select_different_options():
    """Tier 2: ``n`` (one-shot deny) and ``N`` (deny + PERSIST) sit in the
    SAME set (``generic_yn_choices``) — this is a permission-band
    distinction, not a display nuance, so the panel must never collapse
    the two."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.add_pending(
            "k1",
            prompt="Proceed?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in generic_yn_choices()
            ],
        )
        await pilot.pause()

        await pilot.press("n")
        await pilot.pause()
        assert _selected_label(panel) == "[n]o"

        await pilot.press("N")
        await pilot.pause()
        assert _selected_label(panel) == "[N]ever", (
            "uppercase N must select the DISTINCT persistent-deny option, "
            "not be treated as the same key as lowercase n"
        )


@pytest.mark.asyncio
async def test_an_undisplayed_character_is_left_alone_deny_side():
    """Tier 2: #4751's own explicit deny-side acceptance item — a character
    that is NOT one of the active pane's own hotkeys must do nothing: the
    selection stays where it was, and the keypress must not be treated as
    "any printable key selects something" (the implementation must not
    become a catch-all)."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.add_pending(
            "k1",
            prompt="Proceed?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in generic_yn_choices()
            ],
        )
        await pilot.pause()
        assert _selected_label(panel) == "[y]es"

        await pilot.press("z")  # not one of y/A/n/N
        await pilot.pause()

        assert _selected_label(panel) == "[y]es", (
            "an unmapped character must not move the selection"
        )


@pytest.mark.asyncio
async def test_hotkey_is_scoped_to_the_active_pane_only():
    """Tier 2: with TWO pending interventions (only one active/focused —
    #3308's own "a new arrival never steals the active tab"), a hotkey
    press must resolve against the ACTIVE pane's own choice set, never a
    background tab's — mirrors ``test_out_of_order_answer_targets_the_
    selected_tab_by_id``'s own "never head-of-queue" property, for
    selection rather than delivery."""
    app = _PanelOnlyApp()
    async with app.run_test() as pilot:
        panel = app.query_one(InterventionPanel)
        panel.add_pending(
            "k1",
            prompt="Proceed?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in generic_yn_choices()
            ],
        )
        await pilot.pause()
        panel.add_pending(
            "k2",
            prompt="Grant file access?",
            detail=None,
            choices=[
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in file_access_choices("/tmp/project")
            ],
        )
        await pilot.pause()

        # k1 is still active (#3308 AC2: k2 arrived in the background).
        assert _selected_label(panel) == "[y]es"
        # 'r' is k2's RECURSIVE hotkey, not one of k1's own (y/A/n/N) — must
        # not touch k1's selection.
        await pilot.press("r")
        await pilot.pause()
        assert _selected_label(panel) == "[y]es", (
            "a hotkey belonging to a BACKGROUND tab's own choices must not "
            "act on the active tab"
        )
