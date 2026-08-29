"""Tier 2: OS invariant tests for #5521 —
``reyn.hooks.fold.observe_drain_task_death``.

Architect's settled prescription (this issue's own thread): observe a
``drain_folded``-running task's death via ``task.add_done_callback`` —
never swallow (``drain_folded`` keeps its own no-try/except contract,
unchanged). Investigation ① (this PR's own body) confirmed the 3 real
callers (``ingress.py`` / ``composed_consumer.py`` / ``external_fire.py``)
are unchanged in count; a 4th caller that drops the inner try/except its
own callback needs would otherwise kill its drain task PERMANENTLY with
zero record anywhere — this file proves the observation now closes that
gap without adding a new isolation layer.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch. Drives a REAL
  ``asyncio.Task`` + a REAL ``EventLog`` — no stand-ins.
- No private-state assertions — drives ``observe_drain_task_death``
  through its own public contract only, and reads the resulting event
  via ``EventLog``'s own public subscriber surface.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from reyn.core.events.events import EventLog
from reyn.hooks.fold import drain_folded, observe_drain_task_death
from tests._support.events import collect_events, settle


async def _raising_dispatch_batch(batch: list) -> None:
    raise ValueError("simulated: callback forgot its own try/except")


async def _quiet_dispatch_batch(batch: list) -> None:
    pass


@pytest.mark.asyncio
async def test_a_callback_that_raises_records_the_drain_task_death() -> None:
    """Tier 2: #5521 accept — a callback that raises (the #5527-shaped
    hazard: a 4th caller of ``drain_folded`` that dropped its own inner
    try/except) makes the wrapping task die, and that death is recorded
    as a REAL ``hook_drain_task_died`` audit-event — driven through a
    REAL ``asyncio.create_task(drain_folded(...))`` + a REAL ``EventLog``,
    no stand-in for either."""
    events = EventLog()
    collected = collect_events(events)
    queue: "asyncio.Queue[int]" = asyncio.Queue()

    task = asyncio.create_task(drain_folded(queue, _raising_dispatch_batch))
    task.add_done_callback(
        functools.partial(observe_drain_task_death, emit_event=events.emit, label="test-caller")
    )
    await queue.put(1)  # wakes drain_folded's blocking get() -> dispatch_batch raises
    await settle(events)

    died = [e for e in collected if e.type == "hook_drain_task_died"]
    assert died, f"expected a hook_drain_task_died event, got {[e.type for e in collected]!r}"
    assert died[0].data.get("label") == "test-caller"
    assert "ValueError" in died[0].data.get("reason", "")

    assert task.done() and not task.cancelled()
    with pytest.raises(ValueError, match="simulated"):
        task.result()  # #5521: the raise still propagated for real — observation, not swallowing


@pytest.mark.asyncio
async def test_a_normal_cancel_does_not_record_a_death() -> None:
    """Tier 2: #5521 accept, deny side — a task ended via ``task.cancel()``
    (the REAL shutdown shape all 3 production callers use: ``stop()``/
    ``aclose()``) must NOT be recorded as a death. Without this, an
    "always record" implementation would pass the raise-side test above
    for the wrong reason, and every ordinary session teardown would emit
    a false alarm."""
    events = EventLog()
    collected = collect_events(events)
    queue: "asyncio.Queue[int]" = asyncio.Queue()

    task = asyncio.create_task(drain_folded(queue, _quiet_dispatch_batch))
    task.add_done_callback(
        functools.partial(observe_drain_task_death, emit_event=events.emit, label="test-caller")
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(events)

    died = [e for e in collected if e.type == "hook_drain_task_died"]
    assert not died, f"a cancelled (normal shutdown) task must not be recorded, got {died!r}"


@pytest.mark.asyncio
async def test_emit_event_none_degrades_silently_without_raising() -> None:
    """Tier 2: #5521 — ``emit_event=None`` (a caller/test double with no
    audit-emit sink wired, same posture ``FsWatcher``'s own
    ``self._emit_event`` already has) must not itself raise or otherwise
    interfere with the task's own real death — observation is
    best-effort, never a NEW point of failure.

    A ``done_callback`` that itself raises does NOT propagate into this
    test's own coroutine — asyncio routes it to the event LOOP's own
    exception handler instead (never awaited, never surfaced as a red
    test on its own) — so this installs a real handler to CAPTURE that,
    the only way this test can actually witness the claim in its own
    docstring. Waits on a REAL ``asyncio.Event`` set by a SECOND
    ``done_callback`` (added right after the one under test — asyncio
    runs a task's done-callbacks in the order they were added, so this
    second one is guaranteed to run only once the first has already run)
    — no ``sleep(N)``, unbounded."""
    loop = asyncio.get_running_loop()
    captured_loop_exceptions: list[BaseException] = []
    original_handler = loop.get_exception_handler()

    def _capture(loop: "asyncio.AbstractEventLoop", context: dict) -> None:
        exc = context.get("exception")
        if exc is not None:
            captured_loop_exceptions.append(exc)

    loop.set_exception_handler(_capture)
    observed_done = asyncio.Event()
    try:
        queue: "asyncio.Queue[int]" = asyncio.Queue()
        task = asyncio.create_task(drain_folded(queue, _raising_dispatch_batch))
        task.add_done_callback(
            functools.partial(observe_drain_task_death, emit_event=None, label="test-caller")
        )
        task.add_done_callback(lambda _t: observed_done.set())
        await queue.put(1)
        await observed_done.wait()
    finally:
        loop.set_exception_handler(original_handler)

    assert not captured_loop_exceptions, (
        f"observe_drain_task_death must not itself raise when emit_event=None "
        f"— the event loop's own exception handler caught: {captured_loop_exceptions!r}"
    )
    assert task.done()
    with pytest.raises(ValueError, match="simulated"):
        task.result()  # the drain task's OWN real death — unrelated to observe_drain_task_death's return
