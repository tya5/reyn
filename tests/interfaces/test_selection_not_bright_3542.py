"""Tier 2: the drag-selection band, as the live App resolves it, is muted —
not the loud default.

#3542, owner-adjudicated: the selection read as too loud against the
conversation. Originally fixed by asking the terminal for a quieter ANSI
slot (`ansi_blue`, not Textual's default `ansi_bright_blue`) — #4840's owner
ruling (2026-08-16) retired that mechanism (reyn no longer defers to the
terminal for any colour it names), but not the INVARIANT: #3542's own
complaint was loudness, and that is still real under reyn's own theme.
Landed as ③ of #4840's own ①→②→③ sequence — only after reyn's own default
theme (②, #4875) existed to supply a value, since mapping straight to
Textual's generic `$screen-selection-background` any earlier (while
`ansi-dark` was still default) would have regressed this fix: `ansi-dark`'s
own value for that token is the loud `ansi_bright_blue` #3542 moved away
from (measured, #4840's own comment thread).

What "loud" means under a full-colour theme changed too. Textual's own
auto-derived default for an unset `screen-selection-background` is
`primary.with_alpha(0.5)` — reyn's own ACCENT at 50% alpha, which is
`ansi_bright_blue`'s exact complaint with a different hue: full intensity,
not muted. So these tests assert the LIVE, App-resolved selection colour is
BOTH distinct from reyn's own `primary` (not the loud auto-derived default)
and legible against its own paired foreground — the same two properties
`ansi_blue`/`ansi_black` used to guarantee via a slot pick, now guaranteed
by `REYN_THEME`'s own `variables` override (`theme.py`).

`text-style: reverse` was considered and rejected (unchanged from #3542's
original reasoning). Textual COMPOSES the selection style onto each cell, so
reverse would let every coloured run — tool rows, amber intervention
headings, dim chrome — become its own background, and the band would
fragment. That answers a complaint nobody made; the complaint was loudness,
not uniformity.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.theme import REYN_THEME
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


@pytest.mark.asyncio
async def test_the_running_app_uses_reyns_own_theme() -> None:
    """Tier 2: non-vacuity — the two tests below only mean something if the
    App actually resolves the selection style through `REYN_THEME`'s own
    override, not Textual's built-in default for an unset token."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        assert app.theme == "reyn"


@pytest.mark.asyncio
async def test_the_selection_is_not_the_loud_auto_derived_default() -> None:
    """Tier 2: the band lands on reyn's own muted `variables` override, not
    Textual's auto-derived `primary.with_alpha(0.5)` — which would be
    #3542's exact complaint (full accent intensity) with a different hue.

    Asserted on the resolved component style rather than on the stylesheet
    text: a rule can be present and lose to something later in the cascade,
    which is the failure mode this repo keeps finding.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()

        styles = app.screen.get_component_styles("screen--selection")
        resolved_bg = styles.background

        primary = app.get_css_variables()["primary"]
        assert resolved_bg.hex.lower() != primary.lower(), (
            "the selection band resolved to reyn's own primary — the "
            "auto-derived Textual default (primary at 50% alpha) leaking "
            "through, which is #3542's original complaint with a new hue"
        )
        assert resolved_bg.a == 1.0, (
            "the selection band is alpha-blended rather than a fully opaque "
            "override — REYN_THEME.variables['screen-selection-background'] "
            "is meant to replace Textual's own alpha-derived default"
        )


@pytest.mark.asyncio
async def test_the_text_inside_the_band_stays_legible() -> None:
    """Tier 2: only the background changed intent; the pairing invariant
    from #3542 stays — a background chosen without its foreground beside it
    is how a readable pair turns into an unreadable one, and the fix for
    "too loud" must not become "now I cannot read it"."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()

        styles = app.screen.get_component_styles("screen--selection")
        expected_fg = REYN_THEME.variables["screen-selection-foreground"]
        assert styles.color.hex.lower() == expected_fg.lower()
