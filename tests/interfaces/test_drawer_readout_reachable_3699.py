"""#3699 — a drawer readout taller than the drawer stays reachable.

The Help pane is 30 non-blank lines; the drawer caps at 12. Measured before
this change, on a 100x30 terminal: 11 lines on screen and 19 not — and the 19
were the keyboard shortcuts, i.e. the entire reason the pane is opened. No
scrollbar, no indication, and no key that moved it.

Same class as #3688's sent queue: a height cap without an overflow rule does
not shorten a region, it deletes the part past the cap. The sweep that found
this one also cleared the siblings — ``OptionList``-backed panes (History /
Model / Agent / the rewind picker / the completion popup) scroll themselves,
so the readouts (Help / Cost / Ctx) were the remaining case.

Two things have to hold, and only together: the content must be scrollable AND
a key must be able to scroll it. A scrollbar the keyboard cannot drive leaves
the content just as unreachable, with an affordance drawn next to it — so the
central gate here presses keys and asks what became visible, rather than
asserting a style or a scroll offset.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.widgets import ContentSwitcher, OptionList

from reyn.interfaces.inline.textual_chat import MenuBar, TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import (
    MENUBAR_KEYS,
    Composer,
    help_pane_lines,
)
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` (the shared test idiom)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> None:
        pass

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _painted(app: TextualChatApp) -> str:
    """What the compositor actually put on screen — the only surface that
    distinguishes a clipped line from a drawn one."""
    return "\n".join(
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()
    )


def _help_lines(app: TextualChatApp) -> "list[str]":
    return [line for line in help_pane_lines(app._app_binding_help()) if line.strip()]


def _visible(app: TextualChatApp, lines: "list[str]") -> "set[str]":
    painted = _painted(app)
    return {line for line in lines if line.strip()[:38] in painted}


@pytest.mark.asyncio
async def test_the_help_pane_is_taller_than_the_drawer_so_this_module_is_not_vacuous() -> None:
    """Tier 2: the premise — Help really does overflow the drawer.

    If Help ever became short enough to fit, every gate below would pass while
    testing nothing. This fails first and says so.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("help")
        for _ in range(4):
            await pilot.pause()

        lines = _help_lines(app)
        assert len(_visible(app, lines)) < len(lines), (
            "the Help pane now fits in the drawer, so the reachability gates "
            "below no longer exercise the overflow they were written for"
        )


@pytest.mark.asyncio
async def test_every_help_line_can_be_reached_from_the_keyboard() -> None:
    """Tier 2: paging through the drawer surfaces every line of the readout.

    The owner-facing contract: what the pane says must be readable, all of it,
    without a mouse. Before this change PgDn moved nothing and 19 of 30 lines
    could not be brought on screen by any key.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("help")
        for _ in range(4):
            await pilot.pause()

        lines = _help_lines(app)
        seen = _visible(app, lines)
        for _ in range(8):
            await pilot.press("pagedown")
            await pilot.pause()
            seen |= _visible(app, lines)

        unreachable = [line for line in lines if line not in seen]
        assert not unreachable, (
            "lines of the Help pane never became visible while paging through "
            f"it, so nothing in the UI can show them: {unreachable}"
        )


@pytest.mark.asyncio
async def test_opening_a_readout_puts_focus_where_the_scroll_keys_land() -> None:
    """Tier 2: the drawer holds focus for a readout pane.

    Scrollable-but-unfocused is the failure mode this half exists to stop: the
    content would be one keypress away from visible and that keypress would go
    somewhere else.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("help")
        for _ in range(4):
            await pilot.pause()

        assert app.focused is app.query_one("#drawer", ContentSwitcher)


@pytest.mark.asyncio
async def test_a_picker_pane_still_focuses_its_list() -> None:
    """Tier 2: the list panes keep the focus they already had.

    ``↑``/``↓`` drive the selection in a picker; taking that focus away to give
    it to the drawer would break the pickers to fix the readouts.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("model")
        for _ in range(4):
            await pilot.pause()

        assert isinstance(app.focused, OptionList)


@pytest.mark.asyncio
async def test_escape_still_returns_to_the_composer_from_a_readout() -> None:
    """Tier 2: the existing keyboard contract survives the new focus target,
    now in TWO presses (#5869, owner request, overturns this file's own
    #3365-era assumption of one press): ``Esc`` still, eventually, always
    reaches the composer from a readout pane — moving focus onto the drawer
    must not create a place Esc does not work from — but the FIRST press now
    lands on the tab row (drawer still open) rather than skipping it.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("help")
        for _ in range(4):
            await pilot.pause()

        drawer = app.query_one("#drawer", ContentSwitcher)
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()

        assert isinstance(app.focused, MenuBar), (
            f"first Esc from the Help pane did not land on the tab row: {app.focused!r}"
        )
        assert drawer.display, (
            "first Esc from the Help pane closed the drawer -- #5869 keeps it open"
        )

        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()

        assert isinstance(app.focused, Composer), (
            f"second Esc (from the tab row) did not return to the composer: {app.focused!r}"
        )


@pytest.mark.asyncio
async def test_up_from_a_readout_reaches_the_tab_row_leaving_the_drawer_open() -> None:
    """Tier 2: #5869 (owner request) -- ``↑`` from a readout pane (the
    ``ScrollableDrawer`` itself holds focus, #3699) moves focus to the tab
    row without closing the drawer. Before this rung existed ``↑`` from here
    reached NEITHER a scroll action NOR any bound key at any ancestor --
    measured directly (a real ``run_test`` pilot press left focus exactly
    where it started) -- so this is a genuinely new destination, not a
    redirect of a working one.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("help")
        for _ in range(4):
            await pilot.pause()

        drawer = app.query_one("#drawer", ContentSwitcher)
        await pilot.press("up")
        for _ in range(4):
            await pilot.pause()

        assert isinstance(app.focused, MenuBar), (
            f"↑ from the Help pane did not land on the tab row: {app.focused!r}"
        )
        assert drawer.display, "↑ from the Help pane closed the drawer -- #5869 keeps it open"


@pytest.mark.asyncio
async def test_up_from_the_top_of_a_picker_reaches_the_tab_row_leaving_the_drawer_open() -> None:
    """Tier 2: #5869 (owner request) -- ``↑`` on an ``OptionList`` picker's
    FIRST row moves focus to the tab row instead of the base widget's own
    default (wrap to the LAST row -- measured directly, a real pilot press
    on an unnavigated list; never a documented or tested destination this
    app depended on). Every OTHER row still navigates exactly as before --
    this rung fires only at the boundary.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("menu")
        for _ in range(4):
            await pilot.pause()

        opt = app.query_one("#menu", OptionList)
        assert opt.option_count > 1, "setup: the menu pane needs more than one row to test wrap vs. escape"
        drawer = app.query_one("#drawer", ContentSwitcher)

        await pilot.press("up")
        for _ in range(2):
            await pilot.pause()

        assert isinstance(app.focused, MenuBar), (
            f"↑ from the picker's first row did not land on the tab row: {app.focused!r}"
        )
        assert drawer.display, "↑ from the picker's first row closed the drawer -- #5869 keeps it open"


def test_the_help_text_says_how_to_scroll_itself() -> None:
    """Tier 2: the scroll keys appear in the pane whose content is cut off.

    The pane that needed scrolling was also the pane that would have said how —
    so a fix that scrolls without documenting it leaves the discovery problem
    exactly where it was.
    """
    keys = {key for key, _label in MENUBAR_KEYS}
    # Matched case-insensitively and on the abbreviation the tables actually
    # use. The first version tested for "PgDn" or "pagedown" — neither of which
    # is how the ledger spells it (#3805 settled on lowercase `pgdn`), so the
    # assertion was about a spelling rather than about whether paging is
    # mentioned at all, and it went red on a change that made the ledger MORE
    # consistent.
    assert any("pgdn" in key.lower() or "pagedown" in key.lower() for key in keys), (
        f"the drawer's key ledger does not mention paging: {sorted(keys)}"
    )


def test_the_help_text_names_both_esc_rungs() -> None:
    """Tier 2: #5869 -- the Help ledger says what BOTH presses do, not just
    the second one. Before this PR one row ("esc close drawer") already
    covered the pane's own Esc, worded for the OLD one-press behaviour; this
    asserts the NEW two-rung wording is actually there, on both the pane-
    context and tab-row-context rows, rather than assuming a row exists
    just because the key does.
    """
    labels_by_key: "dict[str, list[str]]" = {}
    for key, label in MENUBAR_KEYS:
        labels_by_key.setdefault(key, []).append(label)

    esc_labels = labels_by_key.get("esc", [])
    assert any("back to tabs" in label for label in esc_labels), (
        f"no esc row says the pane-context destination (tab row): {esc_labels!r}"
    )
    assert any("close drawer" in label and "composer" in label for label in esc_labels), (
        f"no esc row says the tab-row-context destination (close + composer): {esc_labels!r}"
    )

    up_labels = labels_by_key.get("↑", [])
    assert any("back to tabs" in label for label in up_labels), (
        f"no ↑ row says the pane-context destination (tab row): {up_labels!r}"
    )
