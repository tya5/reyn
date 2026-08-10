"""#3522 — the composer placeholder reads as an invitation, not as typed text.

Textual's own rule is ``.text-area--placeholder { color: $text 40%; }``. Under
the ``ansi-dark`` theme adopted in #3505 that is INERT: ``$text`` resolves to the
``ansi_default`` marker and alpha compositing drops the marker, so the 40% never
applies and the placeholder painted at the terminal's full default foreground.

Asserted on the PAINTED segment rather than on the CSS declaration, because the
declaration is exactly what was already there and already not working. Two
properties are pinned, not one: that the placeholder IS dimmed, and that typed
text is NOT — a rule reaching further than the placeholder would make the user's
own input look provisional, and only the second assertion can see that.

The colour is deliberately left as the terminal's default (``dim`` rather than a
muted colour): the owner's standing direction is that the terminal's own theme
wins over any concrete shade reyn could pick.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
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


def _body_segment(composer: Composer):
    """The placeholder/text run of the composer's first row.

    NOT ``segments[0]``: the first non-blank segment is the CURSOR CELL, which
    carries its own colour and is unaffected by the placeholder's component
    style. Reading it makes even a ``color: red`` positive control look inert —
    the mistake this helper exists to keep out of the assertions.
    """
    segments = [s for s in composer.render_line(0) if s.text.strip()]
    assert len(segments) > 1, (
        f"expected a cursor cell followed by a text run, got {segments!r}"
    )
    return segments[1]


@pytest.mark.asyncio
async def test_the_placeholder_is_dimmed() -> None:
    """Tier 2b: the empty composer's prompt paints dimmed, so an untouched
    input is legible as untouched."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.text == "", "setup: the composer was not empty"

        assert _body_segment(composer).style.dim, (
            "the placeholder painted at full brightness — Textual's own "
            "`color: $text 40%` is inert under ansi-dark"
        )


@pytest.mark.asyncio
async def test_typed_text_is_not_dimmed() -> None:
    """Tier 2b: the dimming stops at the placeholder.

    The invariant, not the addition: a rule that reached the composer's real
    content would make everything the user types look provisional, and the
    dimmed-placeholder assertion alone cannot tell the two cases apart.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.text = "a real message"
        await pilot.pause()

        assert not _body_segment(composer).style.dim, (
            "the user's own typed text was dimmed along with the placeholder"
        )


@pytest.mark.asyncio
async def test_the_placeholder_keeps_the_terminal_foreground_colour() -> None:
    """Tier 2b: dimming does not pin a colour.

    A muted grey would dim it too, but it would also override whatever
    foreground the user's terminal theme chose — the standing direction is that
    the terminal's theme wins. ``dim`` is the only measured option that darkens
    while leaving the hue to the terminal.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        colour = _body_segment(app.query_one(Composer)).style.color

        assert colour is not None and colour.is_default, (
            f"the placeholder pinned a concrete foreground colour ({colour!r}) "
            "instead of deferring to the terminal's own theme"
        )
