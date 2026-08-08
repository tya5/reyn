"""A full-viewport text effect over the conversation pane, on a key (#3796).

A joke. It draws a TerminalTextEffects animation across the FlowView's viewport
and stops on the same key, leaving the feed exactly where it was.

Not a screensaver, despite the issue's original title. It was one until the
operator changed the trigger:

    「ジョークだし、アイドルスクリーンセーバじゃなくて、キーバインドで開始終了にしようか」

That one change removed three of the five design questions the issue opened
with — idle detection, "never start during a running turn", and opt-in default —
because all three were about *starting without being asked*. **The risk lived in
the trigger, not in the effect.** Worth recording: the same feature, triggered
explicitly, needs almost none of the safety machinery it needed when it decided
for itself.

Why the frames are renderables and not raw ANSI
-----------------------------------------------
The effect is painted through ``FlowView.overlay``, which takes a Rich
renderable and is **non-destructive**: the model, scroll position and both
cursors are untouched, so stopping restores the exact prior view (upstream's
guarantee, `textual-flowview` 0.15.3). TerminalTextEffects yields whole-screen
ANSI strings, which ``Text.from_ansi`` converts — the upstream
``examples/screensaver.py`` is this same composition, and its existence is what
settled the alternative (take the screen with raw ANSI, freezing the feed).

Iterating the effect is enough; ``terminal_output()`` — TTE's own context
manager — must NOT be used here. Measured: inside it, TTE writes cursor-control
sequences to stdout, which is Textual's screen. Outside it, iteration yields the
same frames and writes nothing (0 bytes captured).

Frame rate
----------
:data:`DEFAULT_FPS` is 10, not the 30 the upstream example uses. Measured on
macOS at 100x24, per frame (generation + ``Text.from_ansi`` + Rich render), as a
distribution rather than a mean — the cost changes over an effect's life, so a
mean says more about how many frames were sampled than about the effect:

    effect   median    p90      sustainable fps (median / p90)
    beams    41.7ms   109.1ms          24.0 / 9.2
    rain     18.4ms    25.2ms          54.4 / 39.7
    slide    52.1ms   188.9ms          19.2 / 5.3

**30 fps is not reached by any of the three, even at their median.** Shipping a
default the machine cannot deliver is the kind of unpredictability the operator
rejects elsewhere, so the default is one the machine can hold.

10 is NOT "no dropped frames" — slide's slowest tenth needs ~5 fps, and a
default that slow is not worth looking at. What 10 buys is: every effect's
MEDIAN frame is comfortably inside the interval, so the drops are a hitch in the
slowest tenth rather than the steady state.

Not measured: over ssh (the byte counts above are raw frame sizes, not what a
terminal's differential update actually sends), and anything on Windows/git-bash
— which is where the operator runs reyn.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from rich.console import RenderableType

#: Frames per second for the overlay. See the module docstring for the
#: measurement this is chosen from — it is deliberately below the upstream
#: example's 30, which no measured effect reaches.
DEFAULT_FPS = 10

#: What the effect animates. Short: an effect renders its text INTO the
#: viewport, so a long banner is either clipped or shrinks the animation to a
#: crawl of tiny glyphs.
BANNER = "reyn"

#: The optional dependency this needs. Not a reyn dependency — whether to take
#: one on for a joke is an open question on #3796, so this module works out
#: whether it is there rather than assuming it.
_DEP = "terminaltexteffects"


def available() -> bool:
    """Whether the effect library is importable.

    Checked through ``find_spec`` rather than a try/import: this is asked on the
    key press, and importing the library to answer "is it here" would pay the
    import on a press that is about to say "no".
    """
    import importlib.util

    return importlib.util.find_spec(_DEP) is not None


def unavailable_message() -> str:
    """What to tell the operator when the library is absent.

    Names the install, because "unavailable" without a remedy is a dead end —
    and this is the one failure mode of the feature that is not a bug.
    """
    return (
        f"text effects need the optional {_DEP} package — "
        f"pip install {_DEP}"
    )


def frame_factory() -> "Callable[[int, int], Iterator[RenderableType]]":
    """A ``(width, height) -> frames`` factory for ``FlowView.play_overlay``.

    Re-invoked per loop cycle and on resize, so picking the effect INSIDE means
    every cycle is a different one at the current size — upstream's own idiom,
    and the reason the factory rather than a frame list is the interface.

    Imports the library lazily, at the first press: reyn does not depend on it,
    and a module-level import would make an absent optional dependency a broken
    module rather than a feature that says it is not installed.
    """
    import random

    from rich.text import Text
    from terminaltexteffects.effects import (
        effect_beams,
        effect_rain,
        effect_slide,
    )

    effects = [effect_beams.Beams, effect_rain.Rain, effect_slide.Slide]

    def frames(width: int, height: int) -> "Iterator[RenderableType]":
        effect = random.choice(effects)(BANNER)
        effect.terminal_config.canvas_width = width
        effect.terminal_config.canvas_height = height
        # Iterated directly — see the module docstring on why
        # ``terminal_output()`` must not wrap this.
        for frame in effect:
            yield Text.from_ansi(frame)

    return frames
