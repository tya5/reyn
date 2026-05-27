"""Tier 2: SlashPicker rows are clickable — Slack/Discord muscle memory.

Mouse-interaction UX audit (MED severity Finding F3): the picker
rendered every row as a flat ``Text`` inside one ``Static`` widget,
so clicks fell through to a no-op handler. Users with Slack/Discord
muscle memory clicked a suggestion expecting it to be inserted; nothing
happened.

The fix:
  1. ``SlashPicker.on_click`` maps the click's content-relative y-offset
     to a row index and updates ``_selected``.
  2. Posts a ``SlashPicker.Clicked`` message.
  3. ``InputBar.on_slash_picker_clicked`` routes through the existing
     ``_confirm_picker`` so the keyboard path and mouse path produce
     byte-identical results.

These tests pin both layers — picker-level row mapping + InputBar
end-to-end insertion via pilot.click.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import TextArea

from reyn.chat.slash import SlashCommand
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets.slash_picker import SlashPicker


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _candidates(n: int) -> list[SlashCommand]:
    async def _noop(_session, _args: str) -> None:
        return None
    return [
        SlashCommand(name=f"cmd{i}", summary=f"summary {i}", handler=_noop)
        for i in range(n)
    ]


# ── picker-level: select_at_y ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_select_at_y_moves_selection_to_clicked_row() -> None:
    """Tier 2: ``select_at_y`` moves ``_selected`` to the given row.

    Validates the y-to-row mapping in isolation, without depending on
    pilot click coordinate translation.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        picker.set_matches(_candidates(5))
        await pilot.pause()
        assert picker.selected_index == 0

        # Hit row 3 directly
        assert picker.select_at_y(3) is True
        assert picker.selected_index == 3
        assert picker.selected_command().name == "cmd3"

        # Hit row 0 — back to top
        assert picker.select_at_y(0) is True
        assert picker.selected_index == 0


@pytest.mark.asyncio
async def test_select_at_y_rejects_out_of_range() -> None:
    """Tier 2: clicks below the last row (e.g. on "+N more" footer) return False."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        picker.set_matches(_candidates(3))
        await pilot.pause()

        # Beyond the last row → no change, returns False
        assert picker.select_at_y(99) is False
        assert picker.select_at_y(3) is False     # one past last (idx 2)
        assert picker.selected_index == 0


@pytest.mark.asyncio
async def test_select_at_y_no_op_when_empty() -> None:
    """Tier 2: with no matches loaded, any click is a no-op."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        # No matches set → empty
        assert picker.select_at_y(0) is False


# ── InputBar wiring: clicked message → confirm path ──────────────────────────


@pytest.mark.asyncio
async def test_picker_clicked_message_routes_to_confirm_picker() -> None:
    """Tier 2: posting ``SlashPicker.Clicked`` inserts ``/<name> `` into the TextArea.

    Drives the message-handler path directly without relying on pixel
    click coordinates — that's covered by the picker-level tests above.
    Pins the contract that mouse and keyboard go through the same
    ``_confirm_picker`` so a future refactor of the keyboard path
    automatically inherits the mouse fix.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.click("#input")
        await pilot.press("slash", "l", "i")        # type "/li"
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        assert picker.has_matches
        # Move selection to second match so we test the "selected != 0" case
        if picker._matches[1:]:
            picker.move_selection(+1)
            expected = picker._matches[1].name
        else:
            expected = picker._matches[0].name

        picker.post_message(SlashPicker.Clicked())
        await pilot.pause()

        ta = app.query_one("#input", TextArea)
        assert ta.text == f"/{expected} ", (
            f"expected /<{expected}> in textarea; got {ta.text!r}"
        )
        # Picker hides after confirm
        assert not picker.visible_


@pytest.mark.asyncio
async def test_picker_click_keeps_focus_on_input() -> None:
    """Tier 2: after a row click, focus must still be on the TextArea.

    The user clicked to *select a command* — not to focus the picker
    (which is ``can_focus = False``). The mouse path must end with the
    TextArea ready to accept the rest of the typed args.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.click("#input")
        ta = app.query_one("#input", TextArea)
        assert app.focused is ta

        await pilot.press("slash", "l")
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        assert picker.has_matches

        # Post a click message — handler should re-focus the TextArea
        picker.post_message(SlashPicker.Clicked())
        await pilot.pause()

        assert app.focused is ta, (
            f"input focus lost after picker click; got {app.focused}"
        )
