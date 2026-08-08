"""Tier 1/2: the cached, cancellable text effect (#3860 follow-up).

The operator's report was that the effect stuttered: every frame was generated
on the event loop as it was drawn. The change generates the whole effect ONCE,
on a worker thread, and plays it from a list — so a tick costs a lookup.

Four properties come out of that and each can fail silently:

- **The wait is covered.** Generation takes seconds, so something has to be on
  screen meanwhile, and the handover must land on a pulse-cycle boundary — a
  fade cut in half reads as a glitch, which is what the change is removing.
- **A build is cancellable.** The key is a toggle; pressing it during a build
  must stop the work, not just hide it. A thread that ran on would hold a core
  for several seconds producing frames for a screen nobody is looking at.
- **The pulse eases.** A linear fade reads as a level being turned down; the
  operator asked for a breath.
- **A failed build does not vanish.** The first version ended the overlay the
  instant a build produced no frames — which, after the operator has been
  watching a long pulse (owner: 「pulse 長いと期待が膨らむというガチャ的要素にはなる
  かな」 — length read as anticipation, not overhead), reads as the wait having
  failed rather than as a result. A DIFFERENT effect is retried, bounded, on
  the SAME pulse; only if every attempt fails does the overlay end, and even
  then with a held, legible frame rather than a silent cut.

Timing is not pinned. How LONG a build takes is the host's business — what is
pinned is that the pulse covers it and that the handover is aligned.

The failure tests inject a REAL class shaped like a TTE effect (same
constructor signature, same ``terminal_config``/``__iter__`` contract) that
raises — not a mock of ``_CacheBuilder`` or ``frame_factory``, both of which
would test the injection rather than the generator's own handling of it.
"""
from __future__ import annotations

import threading
import time
from collections import deque

import pytest

from reyn.interfaces.inline.textual_chat import text_effect

requires_tte = pytest.mark.skipif(
    not text_effect.available(),
    reason="optional terminaltexteffects not installed (#3796 ⑤: it is an extra)",
)

_COVERED = ["● a reply on screen", "", "▸ read_file(path=README.md)"] * 4


class _RaisingEffect:
    """A real TTE-shaped effect that always fails, immediately.

    Same construction/iteration contract ``_CacheBuilder`` drives a genuine
    effect through (``cls(art)``, ``.terminal_config.canvas_{width,height}``,
    ``for frame in effect``) — this is not a stand-in for ``_CacheBuilder``
    itself, it is a stand-in for the one thing genuinely outside reyn's
    control: a specific effect misbehaving on specific input.
    """

    class terminal_config:
        canvas_width = 0
        canvas_height = 0

    def __init__(self, art: str) -> None:
        self.art = art

    def __iter__(self):
        raise RuntimeError("simulated effect failure")


class _SlowRaisingEffect(_RaisingEffect):
    """Like :class:`_RaisingEffect`, but only fails after doing real work —
    the shape a resource failure (memory pressure building a large cache)
    would actually take, as opposed to a constructor-time raise."""

    def __iter__(self):
        time.sleep(0.3)
        raise RuntimeError("simulated failure after real work")


def test_the_pulse_returns_to_where_it_started() -> None:
    """Tier 1: one cycle ends adjacent to its own beginning.

    The cycle is played back to back for as long as the build takes, so a cycle
    whose last frame is far from its first would show a jump once a second — the
    seam being visible is the difference between a breath and a blink.
    """
    colours = text_effect._pulse_colours(dark=True)
    assert len(colours) == text_effect.WAITING_FRAMES

    def rgb(c: str) -> tuple:
        return tuple(int(c[i : i + 2], 16) for i in (1, 3, 5))

    first, last = rgb(colours[0]), rgb(colours[-1])
    trough = rgb(colours[len(colours) // 2])
    # The extremes are the extremes: brightest at the start, darkest halfway.
    assert sum(first) > sum(trough), "the pulse does not darken at its middle"
    # And the last frame is nearer the first than the trough is — the cycle is
    # on its way back up, so the next cycle's first frame continues the motion.
    assert abs(sum(last) - sum(first)) < abs(sum(trough) - sum(first))


def test_the_pulse_is_eased_not_linear() -> None:
    """Tier 1: the operator asked for 強弱 — the step between frames varies.

    A straight ramp has one step size, and reads as a dimmer being turned. The
    cosine's steps are small at the extremes and large in between, which is what
    makes it read as breathing.
    """
    colours = text_effect._pulse_colours(dark=True)

    def lum(c: str) -> int:
        return sum(int(c[i : i + 2], 16) for i in (1, 3, 5))

    steps = [abs(lum(b) - lum(a)) for a, b in zip(colours, colours[1:])]
    assert max(steps) > 2 * min(steps), (
        f"the fade is close to linear — steps {steps}"
    )


def test_rewind_steps_through_the_cache() -> None:
    """Tier 1: the reverse pass samples the cache rather than replaying it.

    Both look like a fast rewind on screen; only one keeps a tick at a single
    lookup. Raising the frame rate instead would spend exactly the budget this
    change buys back.
    """
    frames = list(range(20))
    play = text_effect._play(frames)
    forward = [next(play) for _ in range(20)]
    assert forward == frames

    back = [next(play) for _ in range(4)]
    assert back == [18, 13, 8, 3], f"the rewind is not stepping: {back}"


@requires_tte
def test_the_pulse_covers_the_build_and_hands_over_on_a_boundary() -> None:
    """Tier 2: pulse frames until the cache is ready, then effect frames — and
    the switch lands where a cycle ends.

    Driven at the real frame rate, because the worker needs wall time exactly as
    it does in production. Pulling as fast as the loop allows would starve the
    thread and measure a generator that never hands over — which is what the
    first version of this check did.
    """
    # A pulse frame wears one of the cycle's own colours as its whole style.
    # Identified by that rather than by "has no spans" — the first attempt used
    # the span count, and an effect frame that happens to carry no spans read as
    # a pulse frame, putting the handover three frames later than it was. The
    # discriminator has to name the thing, not a side effect of it.
    pulse_styles = set(text_effect._pulse_colours(dark=True))
    generator = text_effect.frame_factory()(60, len(_COVERED), _COVERED)
    kinds: list[str] = []
    try:
        # Unbounded on purpose (testing.md § Time): the condition is "the
        # handover happened", and a build that never finishes should hang here
        # rather than pass a bounded loop that gave up early.
        while "cache" not in kinds:
            frame = next(generator)
            kinds.append("pulse" if str(frame.style) in pulse_styles else "cache")
            time.sleep(1 / text_effect.DEFAULT_FPS)
    finally:
        generator.close()

    handover = kinds.index("cache")
    assert handover > 0, "no pulse was shown while the cache was being built"
    assert handover % text_effect.WAITING_FRAMES == 0, (
        f"the handover cut a pulse cycle at frame {handover}"
    )


@requires_tte
def test_closing_the_overlay_cancels_the_build() -> None:
    """Tier 2: dismissing mid-build stops the worker.

    ``close()`` is what a toggle-off does to this generator — flowview drops its
    reference on ``stop_overlay``. The thread is a daemon, so a leak would not
    hold the process open; it would just burn a core for several seconds, which
    nothing in a suite would notice.
    """
    baseline = threading.active_count()
    generator = text_effect.frame_factory()(60, len(_COVERED), _COVERED)
    next(generator)  # starts the worker
    assert threading.active_count() > baseline, "no build thread was started"

    generator.close()

    # Unbounded: the worker checks its flag once per frame, so this settles in
    # one frame's time or something is wrong with the cancellation itself.
    while threading.active_count() > baseline:
        time.sleep(0.02)


@requires_tte
def test_a_failed_build_is_retried_with_a_different_effect() -> None:
    """Tier 2: one effect failing is not the operator's problem.

    The pool for one press is DISTINCT effects (``random.sample``, not
    ``random.choice`` repeated) — retrying the SAME failing effect would just
    fail again for the same reason. Verified end to end: with the failing
    class first in the pool and a real effect second, the real effect plays.
    """
    from terminaltexteffects.effects import effect_rain

    import reyn.interfaces.inline.textual_chat.text_effect as mod

    original = mod.effect_classes
    mod.effect_classes = lambda: [_RaisingEffect, effect_rain.Rain]
    try:
        generator = text_effect.frame_factory()(60, len(_COVERED), _COVERED)
        frames = []
        try:
            for _ in range(300):
                frames.append(next(generator))
                time.sleep(1 / text_effect.DEFAULT_FPS)
        finally:
            generator.close()
    finally:
        mod.effect_classes = original

    # The real effect actually ran: its frames differ from one another (a
    # held static screen, which is what a total failure falls back to, would
    # not).
    distinct = len({f.plain for f in frames[10:80]})
    assert distinct > 5, (
        f"only {distinct} distinct frames — the real effect never played"
    )


@requires_tte
def test_every_attempt_failing_hands_back_a_held_legible_screen() -> None:
    """Tier 2: when the whole pool fails, the overlay does not just vanish.

    The first version ``return``ed on the first failure, which ends the
    generator — upstream clears the overlay on the very next tick with no
    signal at all (``OverlayFinished`` fires, but nothing on screen says why).
    After a pulse the operator may have been watching for several seconds,
    that reads as the wait having failed, not as an outcome.
    """
    import reyn.interfaces.inline.textual_chat.text_effect as mod

    original = mod.effect_classes
    mod.effect_classes = lambda: [_RaisingEffect] * text_effect.MAX_BUILD_ATTEMPTS
    try:
        generator = text_effect.frame_factory()(60, len(_COVERED), _COVERED)
        # NOT list(): the generator terminates because every pool member
        # fails, but how many PULSE frames it yields before the fallback is
        # decided by thread-scheduling luck (the GIL's slice interval), not by
        # the test — the same shape #3872 cost the operator three reboots
        # over (lead-coder, PR #3876 review: "measured 413 MB, incidentally
        # small is not designed small"). A ``deque(..., maxlen=N)`` drains the
        # SAME generator but retains only the last N — memory is bounded by
        # ``N``, a constant, regardless of scheduling.
        #
        # N = WAITING_FRAMES is not just a safety margin, it is exact: the
        # fallback loop (the code after every pool attempt has failed) always
        # yields precisely WAITING_FRAMES frames as the generator's LAST
        # frames before StopIteration. A deque of that length therefore holds
        # ONLY the fallback segment, whatever came before it (zero pulse
        # frames or several full cycles) — which also fixes a latent flaw in
        # the style-uniformity check below: with list(), frames[0] could be a
        # PULSE frame from before the last attempt failed, and this test would
        # have been asserting two different things depending on how the race
        # resolved on the run.
        frames = deque(generator, maxlen=text_effect.WAITING_FRAMES)
    finally:
        mod.effect_classes = original

    assert frames, "the overlay ended with nothing shown at all"
    # Exactly the fallback segment, by the maxlen argument above — not "at
    # least", because there is nothing else this deque could hold.
    assert len(frames) == text_effect.WAITING_FRAMES, (
        f"the fallback was shown for only {len(frames)} frame(s)"
    )
    # Legible: the operator's own screen, not a blank or a partial paint.
    for line in _COVERED:
        if line:
            assert line in frames[-1].plain, (
                f"the held frame does not show the covered screen: "
                f"{frames[-1].plain!r}"
            )
    # Distinct from the pulse: every held frame carries the SAME style — no
    # fading across the run — which is what makes "it's over" legible against
    # "still waiting". Every item in this deque IS the fallback (see above),
    # so comparing against frames[0] is now comparing fallback to fallback,
    # not risking a leftover pulse frame at the front.
    first_style = str(frames[0].style)
    assert all(str(f.style) == first_style for f in frames), (
        f"the fallback still looks like the pulse (styles vary): "
        f"{sorted({str(f.style) for f in frames})}"
    )


@requires_tte
def test_a_slow_failure_still_recovers_and_still_falls_back_cleanly() -> None:
    """Tier 2: the retry pays real wall-clock time (a resource-style failure,
    not an instant raise) and the pool is still exhausted correctly.

    Distinct from the fast-failure test above: a build that does real work
    before failing exercises the SAME cancel-on-``finally`` path a genuine
    long-running effect would, once per pool attempt.
    """
    import reyn.interfaces.inline.textual_chat.text_effect as mod

    original = mod.effect_classes
    mod.effect_classes = lambda: [_SlowRaisingEffect] * text_effect.MAX_BUILD_ATTEMPTS
    try:
        generator = text_effect.frame_factory()(60, len(_COVERED), _COVERED)
        t0 = time.perf_counter()
        # deque, not list() — same reasoning as the test above: termination
        # is guaranteed (every pool member fails), but the FRAME COUNT before
        # that is scheduler-decided, not test-decided. maxlen bounds memory by
        # a constant instead of by how lucky this run's thread scheduling is.
        frames = deque(generator, maxlen=text_effect.WAITING_FRAMES)
        elapsed = time.perf_counter() - t0
    finally:
        mod.effect_classes = original

    assert frames, "the overlay ended with nothing shown"
    # Each of MAX_BUILD_ATTEMPTS attempts does ~0.3s of real work before
    # failing — a wildly short total would mean the retries never ran.
    assert elapsed > 0.3 * (text_effect.MAX_BUILD_ATTEMPTS - 1), (
        f"only {elapsed:.2f}s elapsed — the pool did not actually retry"
    )
