"""#3503 — the app paints no ground of its own; the terminal's background shows.

Owner report: the input box and the sent-queue region above it were black. The
cause was NOT those widgets — measured, `#inputrow`, `#inputgutter`, `SentQueue`
and `MenuBar` all already declared `transparent`, and all still PAINTED
`#121212`, because "transparent" means "show what is behind" and what was
behind was the App/Screen's own `$background`. Textual's own `App` CSS is
explicit about it (`App { background: $background }`, with an `&:ansi` variant
using `ansi_default`), so the fix has to happen at the root — which is why it
reaches the whole surface rather than only the two regions named.

These tests assert the RESOLVED background of each chrome region is the
terminal's default rather than a forced colour. A test that only checked the
CSS declaration would have passed before the fix — the declaration was
already `transparent`; the painted colour is the thing that was wrong.

#3505 (owner-approved, 2026-07-30) removed the non-vacuity test that used to
live here asserting the `$panel` overlays (#drawer, #completion) still carry
their OWN background rather than the terminal's default — under the
`ansi-dark` theme #3505 switches to for a different fix, `$panel` itself
resolves to `ansi_default`, so that invariant is no longer true by
construction, not merely harder to measure. See
`src/reyn/interfaces/inline/textual_chat/app.py`'s `on_mount` docstring for
the full mechanism and the removal's own history note in this file.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from rich.color import ColorType

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer, MenuBar
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
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


def _is_terminal_default(widget) -> bool:
    bg = widget.rich_style.bgcolor
    return bg is None or bg.type is ColorType.DEFAULT


@pytest.mark.asyncio
async def test_the_input_row_and_sent_queue_take_the_terminals_background() -> None:
    """Tier 2b: the two regions the owner named — the input box and the
    sent-queue above it — resolve to the terminal's own background, not a
    forced dark colour. The sent queue is populated first so the assertion is
    about a region that is actually being displayed."""
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
            assert _is_terminal_default(widget), (
                f"{name} forces its own background "
                f"({widget.rich_style.bgcolor}) instead of taking the terminal's"
            )


@pytest.mark.asyncio
async def test_the_root_and_menu_row_force_no_background_either() -> None:
    """Tier 2b: the ROOT is where this had to be fixed — a per-widget change
    could not work while the App/Screen painted underneath. The menu row is
    included because it is chrome by the same reasoning."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert _is_terminal_default(app.screen), (
            f"the Screen still paints {app.screen.rich_style.bgcolor} — every "
            "transparent child inherits it, which is the whole bug"
        )
        assert _is_terminal_default(app.query_one(MenuBar))



# #3505 (owner-approved, 2026-07-30) REMOVED
# ``test_overlay_regions_still_carry_their_own_background`` here — not skipped.
# That test asserted a non-vacuity invariant: dropping the app's own ground
# (#3503) must not ALSO flatten the ``$panel`` overlays (#drawer,
# #completion) into the terminal's default. #3505 switches
# ``TextualChatApp``'s theme to ``ansi-dark`` to fix a different residue
# (see app.py's ``on_mount`` docstring), and under that theme ``$panel``
# itself resolves to ``ansi_default`` instead of a literal hex — so
# #drawer/#completion now correctly resolve to the terminal's own
# background too, same as every other chrome region. The invariant this
# test checked is not "true but hard to measure now" — it is permanently
# false by construction under this theme, so there is nothing to
# reactivate later; keeping it as a skip would misread as "pending a
# fix." Whether the drawer/completion popup should get an explicit
# concrete background under ``ansi-dark`` is real-hardware-review-gated,
# owner-deferred work tracked separately, not part of #3505 — a NEW test
# belongs with that follow-up if it lands, not a revived version of this
# one.
