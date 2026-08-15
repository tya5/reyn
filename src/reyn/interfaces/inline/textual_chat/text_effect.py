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
All thirty-seven of TerminalTextEffects' effects (#3882 — the operator's
「全種対応」, following #3860's measurement of all 37, which found every one
resolves back to the text it was given: 0 exclusions on that ground).

#3860 also excluded 25 of the 37 on two OTHER grounds — "SLOW" (p90 < 12 fps)
and "LONG" (> 25s per cycle at the then-default 10 fps) — and both were sound
at the time but rested on a premise #3876 removed: that measurement priced
PER-FRAME GENERATION, paid on the UI thread once per tick. #3876 moved
generation to a background thread, done once per effect before playback starts
(:class:`_CacheBuilder`); what a playback tick now costs is a lookup into the
finished list plus a render of an already-built :class:`~rich.text.Text` —
the SAME cost regardless of which effect built it. Re-measured directly
(#3882, this cache's own frames, not synthetic ones): ``overflow``/``sweep``/
``matrix`` — 1860's three "SLOW" effects — render at 112-118 sustainable fps,
statistically indistinguishable from ``beams``/``rain``/``slide``. **SLOW is
retired as a category**; it measured a cost this design no longer pays.

LONG still exists, but as pure frame-count now, uncoupled from render cost —
so it can be corrected by choosing *which frames to show* instead of by
excluding the effect. See "Frame rate" below.

Deliberately a flat list, same as when it was twelve. The operator has not
narrowed it since seeing it grow to twelve, and narrowing it after seeing 37
should still be a deletion, not a redesign.

**Nothing in the test suite enforces the render-cost or cycle-length
figures**, and that is on purpose: both are wall-clock, so pinning them would
fail on a slower CI host for a reason that has nothing to do with reyn. What
the suite does hold is the property the rest of the design rests on: every
member resolves the covered text back, and :data:`_forward_stride` computes a
correct (not merely plausible) stride for any frame count.

Frame rate
----------
:data:`DEFAULT_FPS` is 20 (#3882, raised from #3860's 10) — matching, not
coincidentally, the fps upstream's own ``examples/screensaver.py`` converged
to independently once IT also moved to a cached, pre-built frame list (its
own commit message: 「TTE の毎フレーム計算が loop-bound のため」20 fps —
the same generation-cost constraint #3876 removed here). Re-measured
render-only cost (post-cache, this design's own frames, not upstream's):
median ~7-8ms, p90 ~8.5ms across every effect sampled including the three
former "SLOW" ones — a 110+ fps ceiling with 5x headroom over 20. 20, not
higher, because the headroom is LOCAL-machine, capture-only measurement: ssh
and a real terminal's differential-update write are unmeasured (same gap
#3860 left open), and upstream's own number is the one data point that
crossed that gap and is still available to lean on.

Even at 20 fps a raw frame count turns into a long cycle for the ANIMATION-
heavy effects (``swarm`` 1658 frames = 83s at 20 fps). :data:`_forward_stride`
brings each effect's cycle under :data:`TARGET_CYCLE_SECONDS` by SKIPPING
frames on the forward leg — the same idea :data:`REVERSE_STRIDE` already uses
for the rewind leg, computed per effect from its own cached frame count
rather than pinned as one constant, because the 37 effects' frame counts span
25 to ~1700. A stride is not a fps increase: it costs nothing extra per tick
(still one lookup, one render, at :data:`DEFAULT_FPS`), it just plays fewer of
the cached frames each cycle — the same trade the rewind leg already makes.
"""
from __future__ import annotations

import math
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from rich.console import RenderableType

#: Frames per second for the overlay. See the module docstring ("Frame rate")
#: for the post-cache measurement this is chosen from.
DEFAULT_FPS = 20

#: The per-cycle ceiling :data:`_forward_stride` normalizes toward. See the
#: module docstring ("Which effects") — #3860 excluded anything over 25s at
#: the OLD 10 fps default; this is the new target at the new default, chosen
#: to keep even the longest cached effect (``swarm``, 1658 frames) under a
#: 2x stride rather than needing an aggressive one that would visibly thin
#: the motion.
TARGET_CYCLE_SECONDS = 45

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

    from reyn.interfaces import palette

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
    """The effects the key rotates through — all thirty-seven TerminalTextEffects
    ships (#3882; the criteria and #3860's earlier twelve-effect history are in
    the module docstring's "Which effects").

    A function rather than a module constant so the optional dependency stays
    optional: importing the classes at module scope would make an absent
    ``terminaltexteffects`` an ImportError on a module reyn imports for the
    ``available()`` check alone.

    Public because the LIST is the thing the operator narrows after seeing it,
    and because a test can then check every member rather than whichever ones a
    random draw happens to produce.
    """
    from terminaltexteffects.effects import (
        effect_beams,
        effect_binarypath,
        effect_blackhole,
        effect_bouncyballs,
        effect_bubbles,
        effect_burn,
        effect_colorshift,
        effect_crumble,
        effect_decrypt,
        effect_errorcorrect,
        effect_expand,
        effect_fireworks,
        effect_highlight,
        effect_laseretch,
        effect_matrix,
        effect_middleout,
        effect_orbittingvolley,
        effect_overflow,
        effect_pour,
        effect_print,
        effect_rain,
        effect_random_sequence,
        effect_rings,
        effect_scattered,
        effect_slice,
        effect_slide,
        effect_smoke,
        effect_spotlights,
        effect_spray,
        effect_swarm,
        effect_sweep,
        effect_synthgrid,
        effect_thunderstorm,
        effect_unstable,
        effect_vhstape,
        effect_waves,
        effect_wipe,
    )

    return [
        effect_beams.Beams,
        effect_binarypath.BinaryPath,
        effect_blackhole.Blackhole,
        effect_bouncyballs.BouncyBalls,
        effect_bubbles.Bubbles,
        effect_burn.Burn,
        effect_colorshift.ColorShift,
        effect_crumble.Crumble,
        effect_decrypt.Decrypt,
        effect_errorcorrect.ErrorCorrect,
        effect_expand.Expand,
        effect_fireworks.Fireworks,
        effect_highlight.Highlight,
        effect_laseretch.LaserEtch,
        effect_matrix.Matrix,
        effect_middleout.MiddleOut,
        effect_orbittingvolley.OrbittingVolley,
        effect_overflow.Overflow,
        effect_pour.Pour,
        effect_print.Print,
        effect_rain.Rain,
        effect_random_sequence.RandomSequence,
        effect_rings.Rings,
        effect_scattered.Scattered,
        effect_slice.Slice,
        effect_slide.Slide,
        effect_smoke.Smoke,
        effect_spotlights.Spotlights,
        effect_spray.Spray,
        effect_swarm.Swarm,
        effect_sweep.Sweep,
        effect_synthgrid.SynthGrid,
        effect_thunderstorm.Thunderstorm,
        effect_unstable.Unstable,
        effect_vhstape.VHSTape,
        effect_waves.Waves,
        effect_wipe.Wipe,
    ]


def _forward_stride(frame_count: int) -> int:
    """How many cached frames :func:`_play` advances per forward-leg tick.

    Mirrors :data:`REVERSE_STRIDE`'s idea — skip cached frames rather than
    raise the tick rate — but computed PER EFFECT from its own frame count,
    because the 37 effects span 25 to ~1700 frames (#3860) and one constant
    stride would either barely touch the short effects or leave the longest
    ones far over :data:`TARGET_CYCLE_SECONDS`.

    ``1`` (every frame shown) unless the effect's raw cycle at
    :data:`DEFAULT_FPS` would exceed the target — so short effects are
    completely unaffected, and only the handful of very long ones are
    thinned, by the smallest integer stride that brings them under the
    target.
    """
    target_frames = TARGET_CYCLE_SECONDS * DEFAULT_FPS
    if frame_count <= target_frames:
        return 1
    return -(-frame_count // target_frames)  # ceil division, no float rounding


def _play(frames: list):
    """Forward, then rewind, forever — the operator's shape for the loop.

    The forward leg is thinned by :func:`_forward_stride` before either leg
    runs, so what gets rewound is what was just shown forward — a rewind over
    frames the operator never saw forward would read as a jump, not a rewind.
    Short effects (stride 1) are unaffected; this only touches the handful
    whose raw cache is longer than :data:`TARGET_CYCLE_SECONDS` can hold.

    Rewinding by STEPPING through the (already-thinned) cache
    (:data:`REVERSE_STRIDE`) rather than by raising the frame rate. Both look
    like a fast rewind; only one keeps a tick at one lookup, and the rate is
    what this whole design is buying back.

    Both ends are trimmed by one frame per pass so the extremes are not held for
    two ticks — a repeated first/last frame reads as a stall in an animation
    that is otherwise always moving.
    """
    stride = _forward_stride(len(frames))
    shown = frames[::stride] if stride > 1 else frames
    while True:
        yield from shown
        yield from shown[-2::-REVERSE_STRIDE]


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
