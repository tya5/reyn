"""Tier 1/2: the cached, cancellable text effect (#3860 follow-up).

The operator's report was that the effect stuttered: every frame was generated
on the event loop as it was drawn. The change generates the whole effect ONCE,
on a worker thread, and plays it from a list — so a tick costs a lookup.

Three properties come out of that and each can fail silently:

- **The wait is covered.** Generation takes seconds, so something has to be on
  screen meanwhile, and the handover must land on a pulse-cycle boundary — a
  fade cut in half reads as a glitch, which is what the change is removing.
- **A build is cancellable.** The key is a toggle; pressing it during a build
  must stop the work, not just hide it. A thread that ran on would hold a core
  for several seconds producing frames for a screen nobody is looking at.
- **The pulse eases.** A linear fade reads as a level being turned down; the
  operator asked for a breath.

Timing is not pinned. How LONG a build takes is the host's business — what is
pinned is that the pulse covers it and that the handover is aligned.
"""
from __future__ import annotations

import threading
import time

import pytest

from reyn.interfaces.inline.textual_chat import text_effect

requires_tte = pytest.mark.skipif(
    not text_effect.available(),
    reason="optional terminaltexteffects not installed (#3796 ⑤: it is an extra)",
)

_COVERED = ["● a reply on screen", "", "▸ read_file(path=README.md)"] * 4


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
