"""Tier 2: in-input feedback when the typed slash token matches no command.

Categorical UX gap fill on the input-bar discoverability axis.
Before this PR, typing ``/xxxx`` (= a slash token that doesn't
prefix any registered command) silently hid the picker. The user
got no in-input signal that the command was invalid — they only
learned on submit, when the backend returned "unknown command".

This adds an unknown-command hint row to ``SlashPicker``:

    unknown /xxxx — did you mean /find /save /help?

The suggestion list comes from ``suggest_for_unknown`` (= already
used by the post-submit ErrorBox path) so the in-input feedback
matches what the user would see if they hit Enter.

Public surfaces tested:
  - ``SlashPicker.set_unknown_hint`` sets the dim-red unknown row
  - Visibility gate: ``hint-active`` class is added, ``visible_``
    stays False (= no keyboard intercept — user keeps typing)
  - ``set_matches`` / ``set_hint`` / ``hide`` all clear unknown
    state (= modes are mutually exclusive)
  - InputBar's ``_update_picker`` flow: typing ``/xxxx`` shows the
    unknown hint; typing ``/<known-prefix>`` swaps to matches
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _picker_text(picker) -> str:
    """Plain text rendered by the picker (= what the user sees).

    Reads ``SlashPicker.rendered_text()`` which caches the last frame
    sent to ``Static.update`` — Textual's ``Static`` has no portable
    accessor for its current renderable across versions.
    """
    return picker.rendered_text()


@pytest.mark.asyncio
async def test_set_unknown_hint_renders_unknown_row() -> None:
    """Tier 2: ``set_unknown_hint`` writes a row reading ``unknown /<typed>``."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        picker.set_unknown_hint("xxxx", ["find", "help"])
        await pilot.pause()
        text = _picker_text(picker)
        assert "unknown" in text
        assert "/xxxx" in text
        assert "/find" in text
        assert "/help" in text


@pytest.mark.asyncio
async def test_unknown_hint_does_not_set_visible_predicate() -> None:
    """Tier 2: unknown hint mode keeps ``visible_`` False (= no keyboard intercept).

    Same contract as the existing ``set_hint`` mode — the picker
    must not steal Enter / Tab / arrow keys, because the user is
    still typing and might fix the command name. ``visible_``
    is the matches-only predicate; unknown-hint mode uses the
    ``hint-active`` CSS class for display gating.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        picker.set_unknown_hint("xxxx", ["help"])
        await pilot.pause()
        assert picker.visible_ is False
        assert picker.has_matches is False
        # The hint-active CSS class is the display gate (display: block).
        assert picker.has_class("hint-active") is True


@pytest.mark.asyncio
async def test_set_matches_clears_unknown_state() -> None:
    """Tier 2: switching back to matches removes the unknown row.

    Typical flow: user types ``/xxxx`` (unknown hint), then deletes
    chars to ``/x`` (= multiple matches), the picker should swap to
    the match list. Pin that the unknown state doesn't leak.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        picker.set_unknown_hint("xxxx", ["help"])
        await pilot.pause()
        assert "unknown" in _picker_text(picker)

        async def _h(session, args):  # dummy handler
            return None

        cmd = SlashCommand(name="real", summary="A real command", handler=_h)
        picker.set_matches([cmd])
        await pilot.pause()
        text = _picker_text(picker)
        assert "unknown" not in text
        assert "/real" in text


@pytest.mark.asyncio
async def test_hide_clears_unknown_state() -> None:
    """Tier 2: ``hide()`` wipes unknown-hint state too."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        picker.set_unknown_hint("xxxx", ["help"])
        await pilot.pause()
        picker.hide()
        await pilot.pause()
        assert "unknown" not in _picker_text(picker)
        assert picker.has_class("hint-active") is False


@pytest.mark.asyncio
async def test_input_bar_typing_unknown_command_shows_hint() -> None:
    """Tier 2: end-to-end — typing ``/xxxx`` into the InputBar shows the unknown hint.

    Drives the InputBar's ``_update_picker`` flow with a typed
    unknown command and verifies the resulting picker render
    contains the suggestion line. This is the gap-fill the PR
    targets: before this change, ``/xxxx`` typed into the input
    would silently hide the picker.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#inputbar", InputBar)
        # Slash commands need to be populated so suggest_for_unknown
        # has something to suggest from. The app wires this up at
        # mount time via update_slash_commands; pull from the
        # registry directly to be safe.
        from reyn.slash import REGISTRY
        input_bar.update_slash_commands(REGISTRY.all_commands())
        await pilot.pause()

        input_bar._update_picker("/xxxxnone")
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        text = _picker_text(picker)
        assert "unknown" in text
        assert "/xxxxnone" in text
        # /help is always appended as the escape hatch.
        assert "/help" in text


@pytest.mark.asyncio
async def test_input_bar_typing_known_prefix_does_not_show_unknown() -> None:
    """Tier 2: typing a known prefix shows matches, not the unknown hint."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#inputbar", InputBar)
        from reyn.slash import REGISTRY
        input_bar.update_slash_commands(REGISTRY.all_commands())
        await pilot.pause()
        # "/h" matches /help (and possibly others) — should be matches,
        # not the unknown hint.
        input_bar._update_picker("/h")
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        text = _picker_text(picker)
        assert "unknown" not in text
        assert "/help" in text


@pytest.mark.asyncio
async def test_empty_slash_token_does_not_show_unknown() -> None:
    """Tier 2: bare ``/`` (token empty) opens the full picker, not unknown hint.

    Empty token = "show me everything" not "I typed an invalid
    command". Pin that the empty-token path skips the unknown
    branch entirely.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#inputbar", InputBar)
        from reyn.slash import REGISTRY
        input_bar.update_slash_commands(REGISTRY.all_commands())
        await pilot.pause()
        input_bar._update_picker("/")
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        text = _picker_text(picker)
        assert "unknown" not in text
        # Full picker is open → ``visible_`` is True.
        assert picker.visible_ is True
