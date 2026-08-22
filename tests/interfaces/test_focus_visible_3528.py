"""#3528 — moving focus into the menu bar is visible on the menu bar.

Before this, the only thing that changed anywhere on screen when focus left the
composer for the menu was the composer's own text cursor disappearing: a cue
that can only be read as an absence. The menu's own `:focus-within` rule was a
brightness step between ``$text-muted`` and ``$text``, which ``ansi-dark``
resolves to the same value, so it had been painting nothing since #3505.

Asserted on the PAINTED tab, and asserted in both directions — a marker that
arrives but never leaves would make every tab look focused after the first
visit, which an arrival-only test cannot see.

This module used to ALSO pin the marker's resolved colour as
``ColorType.DEFAULT``/system-defined (never a concrete truecolor), on the
owner's then-standing direction that ``reverse`` should invert "whatever
the terminal is already using" rather than reyn pinning a shade of its own.
#4840's owner ruling (2026-08-16) retired that direction for reyn's
default theme — ``$foreground``/``$background`` now resolve to concrete
RGB structurally, so nothing under reyn's theme is ``ColorType.DEFAULT``
or system-defined anymore, by construction. That test is REWRITTEN, not
deleted (lead-coder review, #4875): pinning "colour matches reyn's current
`$foreground`" would transcribe the CSS rule as the test (six-questions
Q2), but the ORIGINAL guarantee — the marker changes NO colour of its own,
only inverts what was already there — is a RELATIONSHIP between the
focused and unfocused resolved styles, and that relationship survives
#4840 untouched: `color`/`bgcolor` must be IDENTICAL in both states, only
`text-style` (`reverse`/`bold`) may differ.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.widgets import Tab

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer, MenuBar
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
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
async def test_the_marker_changes_no_colour_of_its_own() -> None:
    """Tier 2b: the cue is an SGR attribute, not a palette entry — re-expressed
    RELATIONALLY (#4840, lead-coder review), not as a pinned value.

    The invariant `reverse` was chosen FOR still holds regardless of any
    theme's specific RGB: `reverse` inverts whatever two colours are ALREADY
    there rather than the CSS declaring a color/bgcolor of its own
    (``MenuBar:focus-within Tab.-active { text-style: reverse bold; }`` — no
    `color`/`bgcolor` property). That is a RELATIONSHIP between the focused
    and unfocused resolved styles — the same tab's `color`/`bgcolor` must be
    IDENTICAL in both states, and only its `text-style` (`reverse`/`bold`)
    may differ — which survives any theme's specific RGB, so it is what
    this test asserts (see the module docstring for what this replaces).
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        unfocused = _active_tab_style(app)
        assert not unfocused.reverse, "setup: the marker was already present"

        await pilot.press("tab")
        await pilot.pause()
        focused = _active_tab_style(app)
        assert focused.reverse, "setup: the marker never appeared"

        assert focused.color == unfocused.color and focused.bgcolor == unfocused.bgcolor, (
            f"the focus marker changed the tab's own colour "
            f"(unfocused={unfocused.color!r}/{unfocused.bgcolor!r}, "
            f"focused={focused.color!r}/{focused.bgcolor!r}) instead of only "
            "inverting whatever colour was already there"
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
