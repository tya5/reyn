"""Tier 2: the #3796 text effect starts, stops, and gives the view back.

The joke's one real hazard is not the effect — it is not getting the screen
back. The operator runs reyn on a company machine, so an effect that could not
be dismissed would be an accident rather than a joke. These tests pin the
dismissal, not the animation.

What is deliberately NOT pinned: what the frames look like. Those come from an
optional third-party library, and asserting on them would make reyn's suite fail
when someone else's effect changes.

``terminaltexteffects`` is optional and is NOT a reyn dependency (#3796 ⑤ is
open). The absent-library path is therefore the one every CI run exercises, and
it is a supported outcome rather than a degraded one — so it gets a test of its
own rather than being the reason the others skip.
"""
from __future__ import annotations

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp, text_effect
from tests.test_textual_chat_copy_rewind_3362 import (
    ScriptedTransport,
    _PickerReadModel,
    _texts,
)

requires_tte = pytest.mark.skipif(
    not text_effect.available(),
    reason="optional terminaltexteffects not installed (#3796 ⑤ undecided)",
)


@pytest.mark.asyncio
async def test_the_key_says_so_when_the_library_is_absent() -> None:
    """Tier 2: with the optional library missing, the key reports it and names
    the install — instead of doing nothing, which reads as a broken key."""
    if text_effect.available():
        pytest.skip("library present; the absent path is what this pins")

    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_toggle_text_effect()
        await pilot.pause()

        assert not app.query_one(FlowView).overlay_active, "an overlay was started without the library"
        said = [t for t in _texts(app) if "effects" in t]
        assert said, f"the key did nothing visible: {_texts(app)}"
        # The EXTRA, not the raw package. An operator told to install
        # `terminaltexteffects` directly gets a working key and never learns the
        # extra exists — which is the extra failing at its one job.
        assert any("reyn[effects]" in t for t in said), (
            f"the message names no extra to install: {said}"
        )
        assert not any("pip install terminaltexteffects" in t for t in said), (
            f"the message routes around the extra: {said}"
        )


@requires_tte
@pytest.mark.asyncio
async def test_the_same_key_starts_and_stops_it() -> None:
    """Tier 2: press once, an overlay is active; press again, it is gone.

    Asserted through ``overlay_active`` — the library's own public read — rather
    than by inspecting reyn state, because reyn keeps none: the overlay IS the
    state, which is the property that makes the toggle safe.
    """
    from reyn.runtime.outbox import OutboxMessage

    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await pilot.pause()
        # Something on screen: the effect acts on the covered rows, so an empty
        # conversation has nothing to animate and ends immediately by design
        # (see the empty-viewport test below).
        app._ingest_frame(OutboxMessage(kind="agent", text="something to dissolve"))
        await pilot.pause()

        app.action_toggle_text_effect()
        await pilot.pause()
        assert app.query_one(FlowView).overlay_active, "the first press started nothing"

        app.action_toggle_text_effect()
        await pilot.pause()
        assert not app.query_one(FlowView).overlay_active, (
            "the second press left the overlay up — the joke is now an accident"
        )


@requires_tte
@pytest.mark.asyncio
async def test_the_feed_is_intact_after_the_effect() -> None:
    """Tier 2: the conversation is unchanged by a round trip through the effect,
    INCLUDING output that arrived while it was up.

    The stronger half is the arrival: the overlay covers the viewport, so a
    reader cannot see whether entries landed. If they were dropped instead of
    merely hidden, every visual check would still pass and the loss would only
    surface later, in a conversation missing a reply.
    """
    from reyn.runtime.outbox import OutboxMessage

    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(OutboxMessage(kind="agent", text="before the effect"))
        await pilot.pause()
        before = list(_texts(app))

        app.action_toggle_text_effect()
        await pilot.pause()
        app._ingest_frame(OutboxMessage(kind="agent", text="arrived during the effect"))
        await pilot.pause()
        app.action_toggle_text_effect()
        await pilot.pause()

        after = _texts(app)
        assert after[: len(before)] == before, (
            f"the effect disturbed what was already there: {before} -> {after}"
        )
        assert any("arrived during the effect" in t for t in after), (
            f"output that landed under the overlay was lost, not hidden: {after}"
        )

# The blank-screen no-op test that used to live here (#3796) is deleted, not
# repaired, following the 0.16.1 pin bump that broke its premise (#3866 review).
#
# What it asserted — "a fresh session does not crash the key" — is real, but
# already covered by a DIFFERENT test for a DIFFERENT reason: falsifying the
# early `if not art.strip(): return` guard (removing it entirely) still did not
# crash, because #3866's retry+fallback logic (test_every_attempt_failing_
# hands_back_a_held_legible_screen, in test_text_effect_cache_3860.py) already
# handles "every pool attempt fails" generally, blank input included. The early
# guard is a skip-3-wasted-attempts optimization, not a distinct safety
# contract — testing it separately was redundant.
#
# A second version of this test asserted the OPPOSITE: that a fresh session's
# welcome placeholder ("reyn / Type a message to start / …") IS something the
# effect animates. That one does not survive CLAUDE.md's six questions either,
# for a sharper reason: reyn's own frame_factory does nothing with `covered`'s
# CONTENT (it joins whatever it is handed) — whether the welcome text shows up
# in `covered` at all is entirely flowview's capture behaviour, not reyn's. A
# test that only fails when a THIRD PARTY changes what it reports is pinning
# that party's property under reyn's name (Q1) — if flowview's capture shape
# moves again, this would blame reyn for something reyn never touched.
