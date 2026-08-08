"""A full-viewport text effect over the conversation pane, on a key (#3796).

A joke. It draws a TerminalTextEffects animation across the FlowView's viewport
and stops on the same key, leaving the feed exactly where it was.

Named for what it is rather than for what it was. The issue opened as a
screensaver and so did this module; the operator changed the trigger:

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
guarantee, `textual-flowview` 0.16.0). TerminalTextEffects yields whole-screen
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

#: The optional dependency this needs, and the extra that carries it.
#:
#: An EXTRA rather than a core dependency (owner ruling, #3796): not everyone who
#: installs reyn should carry an animation library for a joke. The message names
#: the extra, never the raw package — an operator told to install the package
#: directly gets a working key and never learns the extra exists, which is the
#: extra failing at the one job it has.
_DEP = "terminaltexteffects"
_EXTRA = "effects"


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
        f"text effects need the optional '{_EXTRA}' extra — "
        f"pip install 'reyn[{_EXTRA}]'"
    )


def frame_factory() -> "Callable[[int, int, list[str]], Iterator[RenderableType]]":
    """A ``(width, height, covered) -> frames`` factory for ``play_overlay``.

    ``covered`` is what the overlay is hiding — the body text of the rows on
    screen, one string per row, top to bottom. **The effect acts on the
    operator's own conversation**: their last replies dissolve and reassemble,
    rather than a banner appearing over them.

    That argument is the whole of #3796's second round. The first version
    animated a fixed ``"reyn"``, because nothing could answer "what is on
    screen right now" — and the pieces to compute it reyn-side (``row_text``,
    ``scroll_offset.y``) are public, so composing them looked reasonable. It is
    not: a sticky header displaces the top rows, so ``row_text(scroll_y + y)``
    is the wrong row exactly while the reader is scrolled into a long entry,
    and it would agree with the screen on the day it was written. The owner's
    ruling was that FlowView must answer it (「viewport の内容は再構築すべきでは
    ないでしょ」), and upstream 0.16.0 does — so the scroll offset and the
    wrapping never appear on this side at all.

    Re-invoked per loop cycle and on resize, so each cycle picks a fresh effect
    AND re-reads what is on screen — a conversation that moved while the effect
    was running is what the next cycle dissolves.

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

    def frames(
        width: int, height: int, covered: "list[str]"
    ) -> "Iterator[RenderableType]":
        # Newline-joined, and nothing else: ``covered`` is already one string
        # per screen row at this width, so any re-wrapping or stripping here
        # would be reyn deciding again what the screen looks like — the thing
        # the upstream argument exists to prevent. Measured: TTE resolves this
        # input back to exactly the text it was given, blank lines and leading
        # indentation included.
        art = "\n".join(covered)
        # TTE raises on input that is non-empty but has no non-whitespace
        # character — measured: "" is fine, "\n\n\n" and "   " both raise
        # ``ValueError: max() iterable argument is empty`` from its own
        # terminal.py. A viewport with nothing in it produces exactly that
        # shape (blank rows joined by newlines), so this is the FRESH SESSION
        # case, not an exotic one: the first thing an operator can do in a new
        # conversation is press the key, and that crashed.
        #
        # Yielding nothing ends the overlay immediately, so the key is a
        # visible no-op on an empty screen. That is the truthful outcome — the
        # effect acts on what is on screen, and there is nothing there to act
        # on — where substituting a banner would be the exact defect this
        # round of #3796 exists to remove.
        if not art.strip():
            return
        effect = random.choice(effects)(art)
        # Sized to what was covered, not to the widget: a canvas narrower than
        # a covered line CLIPS it (measured — a 100-cell line came back 78),
        # and the effect would then resolve to something the operator can see
        # is not what was there.
        effect.terminal_config.canvas_width = width
        effect.terminal_config.canvas_height = height
        # Iterated directly — see the module docstring on why
        # ``terminal_output()`` must not wrap this.
        for frame in effect:
            yield Text.from_ansi(frame)

    return frames
