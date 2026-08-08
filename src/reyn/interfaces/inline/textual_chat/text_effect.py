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

Which effects
-------------
Twelve of TerminalTextEffects' thirty-seven, chosen by measurement rather than
taste (#3860 — the operator's 「ちょっと種類が少なく感じる」). Every one of the
thirty-seven resolves back to the text it was given, so that is not the filter;
two other properties are:

- **p90 >= 12 fps.** An effect whose slow tenth cannot make the interval hitches
  visibly at :data:`DEFAULT_FPS`.
- **<= 25 seconds per cycle.** ``loop=True`` picks a fresh effect only at the END
  of a cycle, so a long effect does not merely take longer — it *suppresses the
  variety*, which is the thing the operator noticed.

``beams`` was in the original three and fails both (10.5 fps, 29 seconds). It is
worth naming why that mattered more than the count: a third of the rotation was
spending half a minute on one effect, so the *felt* variety was below three even
before the list grew.

Deliberately a flat list. The operator has not seen these yet, and narrowing it
after they do should be a deletion, not a redesign.

**Nothing in the test suite enforces those two criteria**, and that is on
purpose: both are wall-clock figures, so pinning them would fail on a slower CI
host for a reason that has nothing to do with reyn. Measured (#3860) — adding a
93-second effect back to this list leaves the suite green. What the suite does
hold is the property the rest of the design rests on: every member resolves the
covered text back. Anyone adding to this list is choosing the speed and the
length themselves, with the numbers on #3860 as the reference.

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

import math
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from rich.console import RenderableType

#: Frames per second for the overlay. See the module docstring for the
#: measurement this is chosen from — it is deliberately below the upstream
#: example's 30, which no measured effect reaches.
DEFAULT_FPS = 10

#: How many frames one cycle of the waiting pulse takes. At :data:`DEFAULT_FPS`
#: that is a one-second breath — and it is also the interval at which the cache
#: is checked, so the switch to the real effect lands on a cycle boundary and
#: never cuts a fade in half.
WAITING_FRAMES = 10

#: How many cached frames the rewind skips per tick. Rewinding by STEPPING
#: rather than by raising the frame rate: a cached tick costs one lookup, and a
#: 5x rate would cost five times the rendering a second — spending the
#: responsiveness this whole design exists to buy.
REVERSE_STRIDE = 5

#: How many distinct effects a single ``ctrl+l`` press will try before giving
#: up (#3866 follow-up). A build can end with no frames — the worker was
#: cancelled, or the effect raised — and the first version of this treated
#: that as "end the overlay", which after a long pulse reads as the wait
#: having been for nothing rather than as a result. Retrying a DIFFERENT
#: effect turns an effect-specific failure into an invisible extra beat of
#: the same pulse; it does nothing for a host-level failure (e.g. memory
#: pressure building a large cache), which is why this is bounded rather than
#: "try every effect" — a systemic failure should surface, not spend seconds
#: failing at every member of the list before saying so.
MAX_BUILD_ATTEMPTS = 3

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


def _pulse_colours(dark: bool) -> "tuple[str, ...]":
    """One colour per frame of the waiting pulse: peak -> ground -> peak.

    A raised cosine rather than a straight line, for the reason the operator
    named — a linear fade reads as a level being turned down, an eased one reads
    as breathing. The same shape and the same ``blend_rgb`` call the shine band
    uses (``activity_row._shine_ramp``), so the two animations in this interface
    move by one law instead of each inventing its own.

    The text never actually goes out. The trough is
    :data:`~palette.BLINK_GROUND_DARK`, near the terminal's background but not
    on it: a fade that reached the background would blink OFF, and off is what a
    fault looks like.
    """
    from rich.color import Color, blend_rgb

    from reyn.interfaces.inline.textual_chat import palette

    peak = Color.parse(
        palette.BLINK_PEAK_DARK if dark else palette.BLINK_PEAK_LIGHT
    ).get_truecolor()
    ground = Color.parse(
        palette.BLINK_GROUND_DARK if dark else palette.BLINK_GROUND_LIGHT
    ).get_truecolor()
    out: "list[str]" = []
    for i in range(WAITING_FRAMES):
        # A FULL turn of cosine: starts at the peak, reaches the ground at the
        # halfway frame, and would be back at the peak on the frame AFTER the
        # last — so consecutive cycles butt together with no repeated frame at
        # the seam.
        fade = 0.5 + math.cos(2.0 * math.pi * i / WAITING_FRAMES) / 2.0
        c = blend_rgb(ground, peak, cross_fade=fade)
        out.append(f"#{c.red:02x}{c.green:02x}{c.blue:02x}")
    return tuple(out)


def waiting_cycle(covered: "list[str]", *, dark: bool = True) -> list:
    """One cycle of the pulse over ``covered`` — what is shown while the cache
    is still being built.

    The operator's own conversation, breathing. Not a spinner and not a banner:
    the effect that follows acts on this same text, so the wait is that text
    too, and the handover is a change of motion rather than a change of subject.
    """
    from rich.text import Text

    art = "\n".join(covered)
    return [Text(art, style=colour) for colour in _pulse_colours(dark)]


class _CacheBuilder:
    """Builds an effect's whole frame list off the UI thread.

    Why a thread: generation is 3-9 seconds for one effect at a real viewport
    size (measured, 100x23) of CPU work inside TerminalTextEffects' own Python
    loop. On the event loop that is the stutter this change removes; per tick,
    lazily, is what the stutter WAS.

    Cancellable between frames, because the key is a toggle and pressing it
    during a build has to close the overlay at once. Python cannot kill a
    thread, so the loop checks a flag — which is why the check sits inside the
    frame loop rather than around it.
    """

    def __init__(self, cls, art: str, width: int, height: int) -> None:
        self._frames: list = []
        self._done = threading.Event()
        self._cancelled = threading.Event()
        self._thread = threading.Thread(
            target=self._build,
            args=(cls, art, width, height),
            daemon=True,
            name="reyn-text-effect-cache",
        )
        self._thread.start()

    def _build(self, cls, art: str, width: int, height: int) -> None:
        from rich.text import Text

        try:
            effect = cls(art)
            effect.terminal_config.canvas_width = width
            effect.terminal_config.canvas_height = height
            out: list = []
            for frame in effect:
                if self._cancelled.is_set():
                    return
                out.append(Text.from_ansi(frame))
            self._frames = out
        except Exception:  # noqa: BLE001 — a joke must not take the session with it
            self._frames = []
        finally:
            self._done.set()

    @property
    def ready(self) -> bool:
        """The cache is complete AND has frames."""
        return self._done.is_set() and bool(self._frames)

    @property
    def gave_nothing(self) -> bool:
        """The build ended with no frames — cancelled, or the effect raised."""
        return self._done.is_set() and not self._frames

    @property
    def frames(self) -> list:
        return self._frames

    def cancel(self) -> None:
        """Ask the build to stop at its next frame.

        Does not join: the caller is the UI thread, and the worker is a daemon
        holding nothing but its own partial list.
        """
        self._cancelled.set()


def effect_classes() -> list:
    """The effects the key rotates through — twelve, chosen by measurement
    (#3860; the criteria are in the module docstring).

    A function rather than a module constant so the optional dependency stays
    optional: importing the classes at module scope would make an absent
    ``terminaltexteffects`` an ImportError on a module reyn imports for the
    ``available()`` check alone.

    Public because the LIST is the thing the operator narrows after seeing it,
    and because a test can then check every member rather than whichever ones a
    random draw happens to produce.
    """
    from terminaltexteffects.effects import (
        effect_expand,
        effect_highlight,
        effect_middleout,
        effect_pour,
        effect_print,
        effect_rain,
        effect_random_sequence,
        effect_scattered,
        effect_slice,
        effect_slide,
        effect_smoke,
        effect_wipe,
    )

    return [
        effect_expand.Expand,
        effect_highlight.Highlight,
        effect_middleout.MiddleOut,
        effect_pour.Pour,
        effect_print.Print,
        effect_rain.Rain,
        effect_random_sequence.RandomSequence,
        effect_scattered.Scattered,
        effect_slice.Slice,
        effect_slide.Slide,
        effect_smoke.Smoke,
        effect_wipe.Wipe,
    ]


def _play(frames: list):
    """Forward, then rewind, forever — the operator's shape for the loop.

    Rewinding by STEPPING through the cache (:data:`REVERSE_STRIDE`) rather than
    by raising the frame rate. Both look like a fast rewind; only one keeps a
    tick at one lookup, and the rate is what this whole change is buying back.

    Both ends are trimmed by one frame per pass so the extremes are not held for
    two ticks — a repeated first/last frame reads as a stall in an animation
    that is otherwise always moving.
    """
    while True:
        yield from frames
        yield from frames[-2::-REVERSE_STRIDE]


def frame_factory(
    *, dark: bool = True
) -> "Callable[[int, int, list[str]], Iterator[RenderableType]]":
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

    ``dark`` is whether the terminal's background is dark — the app's to answer
    (``App.current_theme.dark``), asked at the press rather than cached here, the
    same way the shine band asks it. It picks the pulse's colour pair: one pair
    cannot serve both grounds, since a near-white peak on a white terminal is
    the pulse disappearing rather than the pulse being subtle.

    Imports the library lazily, at the first press: reyn does not depend on it,
    and a module-level import would make an absent optional dependency a broken
    module rather than a feature that says it is not installed.
    """
    import random

    from rich.text import Text

    effects = effect_classes()

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
        pulse = waiting_cycle(covered, dark=dark)
        # A bounded pool of DISTINCT attempts (#3866 follow-up), not one shot:
        # a build can end with no frames — the worker cancelled, or the effect
        # raised — and yielding nothing after a long pulse reads as the wait
        # having failed rather than as a result. See :data:`MAX_BUILD_ATTEMPTS`
        # for why this is bounded rather than exhaustive.
        pool = random.sample(effects, k=min(MAX_BUILD_ATTEMPTS, len(effects)))
        for cls in pool:
            builder = _CacheBuilder(cls, art, width, height)
            # Sized to what was covered, not to the widget: a canvas narrower
            # than a covered line CLIPS it (measured — a 100-cell line came
            # back 78), and the effect would resolve to something the operator
            # can see is not what was there.
            try:
                while not builder.ready and not builder.gave_nothing:
                    # A WHOLE cycle before re-checking: the handover lands on
                    # a boundary, so the pulse is never cut mid-fade. The cost
                    # of waiting is at most one cycle (~1s at DEFAULT_FPS)
                    # after the cache is ready, which is cheaper than a
                    # visible seam.
                    yield from pulse
                if builder.ready:
                    yield from _play(builder.frames)
                    return
                # gave_nothing: try the next effect in the pool, on the SAME
                # pulse — the operator sees one continuous wait, not a series
                # of restarts.
            finally:
                # Reached when the overlay is dismissed and this generator is
                # closed (cancels an in-flight build), and also on every
                # ordinary loop exit (a no-op against an already-finished
                # build — see _CacheBuilder.cancel). A build left running
                # would hold a core for several seconds producing frames for
                # a screen nobody is looking at.
                builder.cancel()
        # Every attempt in the pool failed. Hand the screen back PLAINLY —
        # unfaded, unmoving — rather than let the overlay vanish on the next
        # tick: that is the operator's own conversation as its own
        # acknowledgement that this pull came up empty, distinct from "still
        # building" (pulsing) and from "playing" (moving) so it cannot be
        # mistaken for either. Held for a WHOLE pulse cycle's worth of frames
        # (one at DEFAULT_FPS, same "one held beat" the pulse itself uses) —
        # a single frame is one tick, ~100ms, which a fast build failure
        # (measured: sub-frame for a constructor-time raise) would render as
        # a flicker rather than a legible screen.
        for _ in range(WAITING_FRAMES):
            yield Text(art)

    return frames
