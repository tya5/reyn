"""Tier 1/2: #4986/#4988 — the "cancel-then-await-then-swallow" class.

``EventLog.stop_dispatch()``/``drain()`` (and 6 more sites, census'd from
there — ``cancellable.py``, ``durability_worker.py``,
``connection_service.py``, ``hooks/ingress.py``, ``interfaces/web/
server.py``, ``mcp/subscription_port.py``) used to cancel a task/future
they own, await it, and catch the resulting ``CancelledError``
unconditionally:

    some_task.cancel()
    try:
        await some_task
    except asyncio.CancelledError:
        pass

``await some_task`` raises ``CancelledError`` for two indistinguishable
reasons: (a) ``some_task``'s own cancellation outcome (the intended
target), or (b) the CURRENT coroutine's own task being independently,
externally cancelled at the same await. An unconditional ``pass``
absorbs both — case (b) makes the enclosing function return NORMALLY
instead of letting a genuine external cancellation propagate, exactly
the shape a generic shutdown sweep (`asyncio.run()`'s / pytest-asyncio's
own end-of-loop `_cancel_all_tasks`) can trigger.

Fixed with the SAME discriminator session.py's own #3377 precedent
already uses: ``asyncio.current_task().cancelling() > 0`` in the except
handler, re-raising when true (Python 3.11+, this repo's own floor).

Witness here is "does the cancellation propagate", per CLAUDE.md's own
floor/ceiling rule — no test in this file writes a duration; every
suspension point is landed via ``await asyncio.sleep(0)``'s documented
"yield exactly once" semantics (zero real time, not a wait), the same
idiom this repo's own asyncio tests already use for this purpose.

Real ``EventLog`` + real ``asyncio.Task`` throughout — no fakes.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog


@pytest.mark.asyncio
async def test_stop_dispatch_propagates_an_external_cancel_of_its_own_caller() -> None:
    """Tier 2: the measured defect's own falsifier. ``stop_dispatch()``'s
    OWN calling task is externally cancelled while suspended exactly at
    its internal ``await task`` (awaiting the consumer task it just told
    to cancel) — the fix must let that propagate, not swallow it.

    Reverting the fix (bare ``except asyncio.CancelledError: pass``, no
    ``cancelling()`` check) turns this red: ``stop_dispatch()`` catches
    BOTH the consumer's own cancellation outcome AND the external cancel
    of its own wrapping task, and returns normally either way — so
    ``await wrapper`` below would return ``None`` instead of raising,
    because a task whose own coroutine swallows an external cancel and
    returns normally is NOT ``cancelled()`` (a real, documented asyncio
    property, not a guess) — confirmed by reverting the fix locally
    before landing this test."""
    events = EventLog()
    events.emit("warmup")
    await events.drain()  # real consumer running, suspended at queue.get()

    wrapper = asyncio.create_task(events.stop_dispatch())
    await asyncio.sleep(0)  # let wrapper run up to its own `await task` and suspend
    wrapper.cancel()  # external cancel of stop_dispatch()'s OWN calling task

    with pytest.raises(asyncio.CancelledError):
        await wrapper


@pytest.mark.asyncio
async def test_stop_dispatch_still_absorbs_the_consumers_own_cancellation() -> None:
    """Tier 2: accept-side — the ORIGINAL, intended case (no external
    cancel of the caller) must still work exactly as before: calling
    ``stop_dispatch()`` normally (not wrapped, not externally cancelled)
    returns cleanly (does not raise), and — #4966's own "stop is not
    never again" guarantee — a LATER ``emit()`` still gets delivered via
    a fresh consumer, all through the public surface, no private-state
    read."""
    events = EventLog()
    delivered: list = []
    events.add_subscriber(lambda e: delivered.append(e.type))
    events.emit("warmup")
    await events.drain()

    await events.stop_dispatch()  # must return cleanly, not raise

    events.emit("after-stop")
    await events.drain()
    assert delivered == ["warmup", "after-stop"]
