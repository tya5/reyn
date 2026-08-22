"""#3503/#4840 — chrome takes the App's own ground; no region forces its own.

Owner report (#3503): the input box and the sent-queue region above it were
black. The cause was NOT those widgets — measured, `#inputrow`, `#inputgutter`,
`SentQueue` and `MenuBar` all already declared `transparent`, and all still
PAINTED `#121212`, because "transparent" means "show what is behind" and what
was behind was the App/Screen's own `$background`. Textual's own `App` CSS is
explicit about it (`App { background: $background }`), so the fix has to
happen at the root — which is why it reaches the whole surface rather than
only the two regions named.

These tests assert the RESOLVED background of each chrome region matches the
App's own `$background`, not a forced colour of its own. A test that only
checked the CSS declaration would have passed before the fix — the
declaration was already `transparent`; the painted colour is the thing that
was wrong.

**#4840 (owner ruling, 2026-08-16) changed WHAT "the App's own ground" is,
not whether this invariant holds.** Until #4840, reyn's default theme was
Textual's own `ansi-dark`, under which `$background` resolves to the
`ansi_default` MARKER (no RGB) — so these tests originally asserted the
resolved colour was `rich.color.ColorType.DEFAULT` (a literal ANSI-marker
propagation check), and #3505's own history note (removed here) explained
why the `$panel` overlays' non-vacuity check couldn't survive that theme
either. Under reyn's own theme (`REYN_THEME`, `.theme.py`) `$background` is
a concrete hex (`#1a1d23`), so `ColorType.DEFAULT` no longer describes
ANYTHING reyn paints by default — the invariant this file protects (a
region takes the ground rather than forcing its own colour) is unchanged,
but the way to observe it changed: compare the widget's resolved colour
against the App's own resolved `$background`, not against a marker type.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.color import Color

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer, MenuBar
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
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


def _matches_app_ground(widget, app: TextualChatApp) -> bool:
    """True iff *widget*'s resolved background is the SAME colour as the
    App's own resolved ``$background`` — i.e. it is taking the ground rather
    than forcing one of its own. Compares resolved RGB, not the CSS
    declaration (``transparent`` was already declared before #3503's fix and
    still painted the wrong colour — the resolved value is the thing that
    was wrong, so it is the thing this checks)."""
    bg = widget.rich_style.bgcolor
    if bg is None or bg.triplet is None:
        return False
    app_bg = Color.parse(app.get_css_variables()["background"]).rich_color
    return bg.triplet == app_bg.triplet


@pytest.mark.asyncio
async def test_the_input_row_and_sent_queue_take_the_apps_ground() -> None:
    """Tier 2b: the two regions the owner named — the input box and the
    sent-queue above it — resolve to the App's own ground, not a forced dark
    colour. The sent queue is populated first so the assertion is about a
    region that is actually being displayed."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        queue = app.query_one(SentQueue)
        queue.show_item("m1", "a queued message")
        await pilot.pause()

        for name, widget in (
            ("#inputrow", app.query_one("#inputrow")),
            ("#inputgutter", app.query_one("#inputgutter")),
            ("Composer", app.query_one(Composer)),
            ("SentQueue", queue),
        ):
            assert _matches_app_ground(widget, app), (
                f"{name} forces its own background "
                f"({widget.rich_style.bgcolor}) instead of taking the App's "
                f"own ground ({app.get_css_variables()['background']!r})"
            )


@pytest.mark.asyncio
async def test_the_root_and_menu_row_force_no_background_either() -> None:
    """Tier 2b: the ROOT is where this had to be fixed — a per-widget change
    could not work while the App/Screen painted underneath. The menu row is
    included because it is chrome by the same reasoning."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert _matches_app_ground(app.screen, app), (
            f"the Screen still paints {app.screen.rich_style.bgcolor} — every "
            "transparent child inherits it, which is the whole bug"
        )
        assert _matches_app_ground(app.query_one(MenuBar), app)


# #3505 (owner-approved, 2026-07-30) REMOVED
# ``test_overlay_regions_still_carry_their_own_background`` here — not skipped.
# That test asserted a non-vacuity invariant: dropping the app's own ground
# (#3503) must not ALSO flatten the ``$panel`` overlays (#drawer,
# #completion) into the terminal's default. #3505 switched
# ``TextualChatApp``'s theme to ``ansi-dark`` to fix a different residue
# (see app.py's ``on_mount`` docstring history), and under THAT theme
# ``$panel`` itself resolved to ``ansi_default`` instead of a literal hex —
# so #drawer/#completion correctly resolved to the terminal's own
# background too, same as every other chrome region, and the removed test's
# invariant was permanently false by construction under it.
#
# #4840 (owner ruling, 2026-08-16) replaces ``ansi-dark`` with reyn's own
# theme (``REYN_THEME``), where ``$panel`` is AGAIN a concrete hex distinct
# from ``$background`` (``#232833`` vs ``#1a1d23`` — chosen explicitly, not
# boost-derived, see ``theme.py``'s own docstring) — the #3505-era
# permanent-falseness this removal note describes no longer holds. Whether
# to REVIVE a #drawer/#completion non-vacuity test against reyn's own
# ``$panel`` is real-hardware/visual-review-gated, tracked with #4840's own
# follow-up, not reintroduced silently here — this file's scope stays the
# root/chrome regions #3503 named.
#
# ★ That removed test was ALSO the file's non-vacuity guard for the two
# tests above: without it, ``test_the_input_row_and_sent_queue_...`` and
# ``test_the_root_and_menu_row_...`` would go green even under a defect
# that resolves EVERYTHING (the whole chrome, no exceptions) to the App's
# ground. The replacement below restores that role with a check that does
# not depend on any theme's specific values: it forces a widget's
# background directly (bypassing CSS/theme entirely) and asserts
# ``_matches_app_ground`` reports it as NOT matching — proving the helper
# (and the render path it reads) can still tell "forced concrete colour"
# apart from "the App's own ground" at all, independent of which theme (or
# which hex) is active.


@pytest.mark.asyncio
async def test_a_forced_concrete_background_is_still_detected_as_distinct() -> None:
    """Tier 2b: non-vacuity guard, theme-independent. Directly overrides one
    widget's resolved background (bypassing CSS/theme resolution entirely,
    so this holds regardless of what any theme resolves ``$panel``/
    ``$background`` to) and asserts ``_matches_app_ground`` reports False for
    it. Falsify: drop the ``widget.styles.background = ...`` line below and
    this goes red, confirming the guard is live, not a tautology."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        widget = app.query_one(MenuBar)
        assert _matches_app_ground(widget, app), (
            "sanity: MenuBar should match the App's own ground BEFORE the "
            "forced override below, or this test proves nothing"
        )
        widget.styles.background = Color.parse("red")
        await pilot.pause()
        assert not _matches_app_ground(widget, app), (
            "forcing a concrete background did not change "
            f"{widget.rich_style.bgcolor!r} away from the App's own ground — "
            "the non-vacuity check itself is broken, independent of any "
            "theme or $panel/$background decision"
        )
