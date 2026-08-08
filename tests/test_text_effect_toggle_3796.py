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

from reyn.interfaces.inline.textual_chat import TextualChatApp, screensaver
from tests.test_textual_chat_copy_rewind_3362 import (
    ScriptedTransport,
    _PickerReadModel,
    _texts,
)

requires_tte = pytest.mark.skipif(
    not screensaver.available(),
    reason="optional terminaltexteffects not installed (#3796 ⑤ undecided)",
)


@pytest.mark.asyncio
async def test_the_key_says_so_when_the_library_is_absent() -> None:
    """Tier 2: with the optional library missing, the key reports it and names
    the install — instead of doing nothing, which reads as a broken key."""
    if screensaver.available():
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


@requires_tte
def test_the_frames_resolve_to_the_covered_text_not_a_banner() -> None:
    """Tier 2: what the effect RESOLVES TO is the covered rows (#3796 round 2).

    The operator found the first version animating a fixed ``"reyn"`` over their
    conversation. My first attempt at a witness for the fix asserted that the
    factory was *handed* the covered rows — and a banner implementation passes
    that, because being handed an argument is not using it. Falsified exactly
    so: reverting the body to ``art = "reyn"`` left that assertion green.

    So this reads the FRAMES. A TTE effect resolves to the text it was given
    (measured: the final frame comes back equal to the input, blank lines and
    indentation included), which makes the last frame the one place the
    argument's fate is observable without pinning a third-party animation's
    intermediate pixels.
    """
    from reyn.interfaces.inline.textual_chat import screensaver

    covered = [
        "user: what does the drawer show?",
        "",
        "● thirteen tabs; Cost and Ctx are readouts",
    ]
    frames = list(screensaver.frame_factory()(78, len(covered), covered))
    assert frames, "the factory produced no frames for a non-empty screen"

    final = frames[-1].plain  # the factory yields rich Text
    for line in covered:
        if line:
            assert line in final, (
                f"the effect resolved to something other than the screen it "
                f"covered — {line!r} is missing from {final!r}"
            )
    assert "reyn" not in final, "a banner leaked into the frames"


@requires_tte
@pytest.mark.asyncio
async def test_an_empty_screen_is_a_no_op_not_a_crash() -> None:
    """Tier 2: the key on a fresh, empty conversation does nothing — and
    specifically does not raise.

    Not an exotic case: an empty viewport is blank rows, which join to
    ``"\n\n\n..."``, and TTE raises ``ValueError`` on input that is non-empty
    but carries no non-whitespace character (measured: ``""`` is accepted,
    ``"\n\n\n"`` and ``"   "`` are not). The first thing an operator can do in
    a new session is press the key, so this was the first thing that crashed.

    A no-op is the truthful outcome rather than a fallback banner: the effect
    acts on what is on screen, and an empty screen has nothing to act on.
    """
    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_toggle_text_effect()  # must not raise
        await pilot.pause()
        assert not app.query_one(FlowView).overlay_active, (
            "an empty screen started an overlay with nothing to animate"
        )
