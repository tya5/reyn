"""#3528 — moving focus into the menu bar is visible on the menu bar.

Before this, the only thing that changed anywhere on screen when focus left the
composer for the menu was the composer's own text cursor disappearing: a cue
that can only be read as an absence. The menu's own `:focus-within` rule was a
brightness step between ``$text-muted`` and ``$text``, which ``ansi-dark``
resolves to the same value, so it had been painting nothing since #3505.

Asserted on the PAINTED tab, and asserted in both directions — a marker that
arrives but never leaves would make every tab look focused after the first
visit, which an arrival-only test cannot see.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.widgets import Tab

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer, MenuBar
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransport):
    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass

    async def deliver_pending_answer(self, text: str) -> bool:
        return False


def _active_tab_style(app):
    """The painted style of the menu's ACTIVE tab — the label a reader looks at
    to answer "am I in the menu, and where?"."""
    for tab in app.query(Tab):
        if tab.has_class("-active"):
            return next(
                segment.style
                for segment in tab.render_line(0)
                if segment.text.strip()
            )
    raise AssertionError("no active tab is rendered in the menu bar")


def _inactive_tab_styles(app):
    return [
        next(
            (segment.style for segment in tab.render_line(0) if segment.text.strip()),
            None,
        )
        for tab in app.query(Tab)
        if not tab.has_class("-active")
    ]


@pytest.mark.asyncio
async def test_focusing_the_menu_marks_the_active_tab() -> None:
    """Tier 2b: arriving in the menu paints a marker that was not there before.

    The assertion is that something is ADDED, not merely that the two states
    differ — the defect being fixed is precisely that the only difference was
    something being taken away elsewhere on screen.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        assert not _active_tab_style(app).reverse, (
            "setup: the active tab was already marked while the composer had focus"
        )

        await pilot.press("tab")
        await pilot.pause()
        assert isinstance(app.focused, MenuBar), (
            f"setup: Tab did not move focus to the menu ({app.focused!r})"
        )
        assert _active_tab_style(app).reverse, (
            "focus reached the menu but the active tab paints exactly as before"
        )


@pytest.mark.asyncio
async def test_leaving_the_menu_clears_the_marker() -> None:
    """Tier 2b: the marker is a focus indicator, not a permanent decoration.

    Without this, the first visit to the menu would leave it looking focused
    for the rest of the session — a cue that never turns off carries no
    information.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert _active_tab_style(app).reverse, "setup: the marker never appeared"

        app.query_one(Composer).focus()
        await pilot.pause()
        assert not _active_tab_style(app).reverse, (
            "the focus marker stayed on the menu after focus returned to the composer"
        )


@pytest.mark.asyncio
async def test_the_marker_forces_no_colour_of_its_own() -> None:
    """Tier 2b: the cue is an SGR attribute, not a palette entry.

    A concrete colour would look the same on every terminal theme, which is the
    thing the app's colour direction rules out; ``reverse`` inverts whatever two
    colours the terminal is already using, so it survives a light theme as well
    as a dark one.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()

        style = _active_tab_style(app)
        assert style.reverse, "setup: the marker never appeared"
        for colour in (style.color, style.bgcolor):
            assert colour is None or colour.is_default or colour.is_system_defined, (
                f"the focus marker pinned a concrete colour ({colour!r}) instead of "
                "inverting the terminal's own"
            )


@pytest.mark.asyncio
async def test_focus_does_not_restyle_the_other_tabs() -> None:
    """Tier 2b: the marker says WHERE you are, not merely that the bar is live.

    If every tab changed on focus the bar would flash as a block and the
    active-tab distinction — the only thing that survived ``ansi-dark`` — would
    be buried under it.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        before = _inactive_tab_styles(app)

        await pilot.press("tab")
        await pilot.pause()
        after = _inactive_tab_styles(app)

        assert before == after, (
            f"focusing the menu restyled the inactive tabs: {before!r} -> {after!r}"
        )
