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

reyn's own CSS (``Composer > .text-area--placeholder { text-style: dim; }``,
``app.py``) picks ``dim`` — an SGR ATTRIBUTE, not a colour — over a muted
grey. This module used to ALSO pin the resolved colour as the terminal's
default (``ColorType.DEFAULT``), on the owner's then-standing direction that
the terminal's own theme should win over any concrete shade reyn could
pick. #4840's owner ruling (2026-08-16) retired that direction — reyn's
default theme now supplies concrete RGB for `$text` itself, so nothing
under it resolves to `ColorType.DEFAULT` anymore, structurally.

The third test here used to pin that absolute value; it is REWRITTEN, not
deleted (lead-coder review, #4875) — pinning "colour matches reyn's `$text`
value" would transcribe the CSS rule as the test (six-questions Q2), but
the ORIGINAL guarantee ("the placeholder sinks", not "the placeholder is
`ColorType.DEFAULT`") is a RELATIONSHIP, not a value, and that relationship
survives #4840 untouched: the placeholder must still be DARKER than typed
text once actually rendered. Resolved via
``textual.filter.dim_color`` — the SAME function #4850's crash investigation
found (``filter.py:129``) — applied to both segments' colour against the
composer's own resolved background, so the comparison is the ACTUAL painted
luminance a `dim=True` segment produces, not the pre-render `Style.color`
(which is identical for both segments — only the `.dim` flag differs at
that level, per the two tests above).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from rich.color import Color as RichColor
from textual.filter import dim_color

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
async def test_the_placeholder_paints_darker_than_typed_text() -> None:
    """Tier 2b: the placeholder must sink relative to real content — a
    RELATIONSHIP (#4840, lead-coder review), not a pinned absolute colour.

    This module used to pin the placeholder's resolved colour as
    `ColorType.DEFAULT`, on the standing-at-the-time direction that the
    terminal's own theme should win over any concrete shade reyn could
    pick. #4840's owner ruling retired that direction for reyn's default —
    `$text` now resolves to concrete RGB structurally, so `ColorType.DEFAULT`
    no longer describes anything reyn paints. The ORIGINAL guarantee this
    protected — "the placeholder reads as an invitation, not as typed
    text" (this module's own title) — was never really about the colour
    TYPE; it was about the placeholder being VISUALLY DARKER than real
    content, and that survives #4840 untouched. Resolved through
    `textual.filter.dim_color` (the same function #4850's crash
    investigation found, `filter.py:129`) against the composer's own
    background, so this compares actual painted luminance, not the
    pre-render `Style.color` (identical for both segments at that level —
    see the two tests above).
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.text == "", "setup: the composer was not empty"
        placeholder_segment = _body_segment(composer)

        composer.text = "a real message"
        await pilot.pause()
        typed_segment = _body_segment(composer)

        background = composer.rich_style.bgcolor or composer.rich_style.color
        assert background is not None, "setup: no resolvable ambient colour"
        placeholder_color = placeholder_segment.style.color
        typed_color = typed_segment.style.color
        assert placeholder_color is not None and typed_color is not None, (
            "setup: a segment painted with no foreground colour at all"
        )

        placeholder_rgb = dim_color(background, placeholder_color)
        typed_rgb = typed_color

        placeholder_luminance = _relative_luminance(placeholder_rgb)
        typed_luminance = _relative_luminance(typed_rgb)
        assert placeholder_luminance < typed_luminance, (
            f"the placeholder ({placeholder_luminance:.3f}) is not darker "
            f"than typed text ({typed_luminance:.3f}) once actually rendered"
        )


def _relative_luminance(color: RichColor) -> float:
    """WCAG relative luminance of a resolved (truecolor) `rich.color.Color` —
    the same formula #4787/#4840's own contrast-measurement comments use,
    inlined here rather than imported (no shared util module owns it)."""
    triplet = color.get_truecolor()

    def lin(component: int) -> float:
        c = component / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = lin(triplet.red), lin(triplet.green), lin(triplet.blue)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
