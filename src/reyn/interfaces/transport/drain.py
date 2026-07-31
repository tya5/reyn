"""The suspension point every frame drain passes through, once per frame (#3570).

A frame drain reads like it suspends per frame and does not: ``asyncio.Queue.get()``
returns WITHOUT suspending while the queue is non-empty, and ``async for`` over a
buffered source (an SSE line iterator, a decoded block) awaits coroutines that
return immediately. Awaiting something that does not suspend is not a scheduling
point, so a producer that delivers several frames at once has the consumer drain
to exhaustion with the event loop never running anything else — no animation, no
keystroke, no timer, for as long as the burst lasts.

:func:`suspend_between_frames` is that missing point, and it lives here rather
than being spelled ``await asyncio.sleep(0)`` at each site for one reason: it
gives the property a NAME that a gate can enumerate. ``tests/
test_stream_drain_yield_3570.py`` walks every ``frames()`` drain in this package
and requires each ``yield`` to be paired with a call to this function, so a
FOURTH transport cannot reintroduce the defect by simply not knowing about it.
Three sites fixed by hand is a fact about today; the pairing is a rule about
tomorrow.
"""
from __future__ import annotations

import asyncio


async def suspend_between_frames() -> None:
    """Return control to the event loop, unconditionally, exactly once.

    Called once per frame by every drain in this package — never conditionally
    ("only when the queue is non-empty" is the timing-dependent shape this
    exists to remove) and never per batch: the queues involved are unbounded,
    so a "suspend every N frames" rule would leave the interval between
    suspensions proportional to something the producer chooses. At one frame it
    is a single frame's processing, which is the smallest a non-preemptible
    consumer can offer.
    """
    await asyncio.sleep(0)


__all__ = ["suspend_between_frames"]
