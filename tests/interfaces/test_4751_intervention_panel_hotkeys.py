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

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widgets import RadioButton, RadioSet, TabbedContent

from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.intervention_choices import file_access_choices, generic_yn_choices


async def _settle_until(pilot, until) -> None:
    """Pump until ``until()`` is true (#3748: unbounded, owner policy) —
    duplicated from ``test_textual_chat_intervention_panel_3299.py``'s own
    helper of the same name (not shared via ``tests/_support`` today; this
    is the second call site).

    #5705 (owner real-machine incident, root cause): ``radio.has_focus``
    lands via a TWO-STAGE deferral — ``add_pending`` posts a
    ``TabbedContent.TabActivated`` message, whose handler
    (``InterventionPanel.on_tabbed_content_tab_activated``) then calls
    ``self.call_after_refresh(radios[0].focus)``, a SECOND deferred step.
    A single ``pilot.pause()`` (this test's own pre-fix shape) relies on
    ``Pilot.pause()``'s ``wait_for_idle`` — a wall-clock/CPU-time heuristic
    ("has this process stopped actively working") — to happen to flush
    both stages before returning. Under real CPU contention (measured:
    the full suite's own ``-n auto`` xdist run, and directly reproduced
    locally by running ONLY this one test alongside unrelated CPU-bound
    background load — 1 failure in 8 runs) the heuristic can be fooled by
    scheduler PREEMPTION into reporting "idle" before the second deferred
    stage has actually run — not a version-specific bug (reproduced under
    contention on 3.11; #5705's own CI evidence showed 3.12 fail too), not
    an ordering dependency on another test's state (this repro used no
    other test at all), and not the ``RuntimeError: Event loop is closed``
    #5705 also observed nearby in one CI log (a separate test's own
    concurrent symptom under the same contention — this repro reproduced
    the ``has_focus`` failure with NO other test running, so that error
    is not this failure's cause).

    Fixed with an UNBOUNDED condition-wait, never a fixed pump COUNT
    (e.g. two ``pilot.pause()`` calls) — a count is a duration floor
    wearing a different unit (#5705 review, lead-coder: "回数の見積もり
    ... CLAUDE.md が禁じる duration の floor と同じ"): a fixed count
    happens to cover today's two-stage chain but reintroduces the exact
    same class of failure the day a third deferral stage is added. The
    ceiling is CI's own ``--timeout=120`` (pytest-timeout), never a
    marker or sleep written into this test."""
    while True:
        await pilot.pause()
        if until():
            return
        await asyncio.sleep(0.01)


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
        # #5705: unbounded settles, not a fixed pump count — see
        # _settle_until's own docstring for the two-stage deferral this
        # waits out. Stage 1: the tab itself must have activated
        # (`tabs.active` non-empty) before `_active_pane` can resolve it
        # at all.
        tabs = panel.query_one(TabbedContent)
        await _settle_until(pilot, lambda: tabs.active != "")
        radio = _active_pane(panel).query_one(RadioSet)
        # Stage 2: the deferred focus call (`call_after_refresh`, fired
        # from stage 1's own message handler).
        await _settle_until(pilot, lambda: radio.has_focus)
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
