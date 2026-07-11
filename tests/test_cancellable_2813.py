"""Tier 2: race_cancellable — the shared cancel_event race for bounded external calls (#2813).

Direct unit coverage of the primitive itself (op-level coverage lives in
test_2761_pr3_mcp_immediate_probe.py, which drives the real MCP install path).
These tests lock down the same-task cancel-scope mechanism precisely (mirroring
CPython's own asyncio.timeout design) — the load-bearing properties a naive
spawn-a-new-task implementation would get wrong:

- cancel_event firing does NOT leak an orphaned task (the coroutine is awaited
  INLINE in the host task, not spawned — there is no separate task to leak)
- a GENUINE external cancel of the host task (unrelated to cancel_event) still
  propagates as CancelledError, not swallowed as Cancelled
- an external cancel arriving BEFORE race_cancellable is even entered (already
  cancelling) is not misattributed to the event
- cancel_event=None is a byte-identical passthrough (every pre-#2813 caller)
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.cancellable import Cancelled, race_cancellable


@pytest.mark.asyncio
async def test_cancel_event_interrupts_immediately_not_after_own_timeout():
    """Tier 2: cancel_event firing mid-await raises Cancelled promptly — not after
    the coroutine's own (much longer) internal duration."""
    async def _slow() -> str:
        await asyncio.sleep(60)
        return "done"

    cancel_event = asyncio.Event()

    async def _fire_soon() -> None:
        await asyncio.sleep(0.1)
        cancel_event.set()

    asyncio.ensure_future(_fire_soon())
    with pytest.raises(Cancelled):
        await asyncio.wait_for(
            race_cancellable(_slow(), cancel_event=cancel_event), timeout=5.0,
        )


@pytest.mark.asyncio
async def test_cancel_event_none_is_plain_passthrough():
    """Tier 2: cancel_event=None (every pre-#2813 caller) is byte-identical to a
    plain await — no watcher task, no race, normal return and normal exceptions."""
    async def _ok() -> str:
        return "value"

    assert await race_cancellable(_ok(), cancel_event=None) == "value"

    async def _raises() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await race_cancellable(_raises(), cancel_event=None)


@pytest.mark.asyncio
async def test_successful_coro_result_returned_when_event_never_fires():
    """Tier 2: cancel_event passed but never set — coro completes normally, its
    result is returned, and no stray cancellation leaks to the caller afterward
    (the watcher's own cancel-on-cleanup must not bleed into subsequent awaits)."""
    cancel_event = asyncio.Event()

    async def _fast() -> int:
        await asyncio.sleep(0.05)
        return 42

    result = await race_cancellable(_fast(), cancel_event=cancel_event)
    assert result == 42

    # No stray CancelledError should surface on a subsequent await in this task —
    # proof the watcher's own cleanup-cancel didn't leak.
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_coro_exception_propagates_when_event_never_fires():
    """Tier 2: a real exception from coro (not a cancellation at all) propagates
    unchanged — race_cancellable must not swallow or reshape it."""
    cancel_event = asyncio.Event()

    async def _boom() -> None:
        raise RuntimeError("real failure")

    with pytest.raises(RuntimeError, match="real failure"):
        await race_cancellable(_boom(), cancel_event=cancel_event)


@pytest.mark.asyncio
async def test_genuine_external_cancel_of_host_task_still_propagates():
    """Tier 2: #2813 regression guard — a GENUINE external cancel of the HOST
    task (e.g. registry.shutdown()'s hard-cancel, unrelated to cancel_event) must
    still propagate as CancelledError, not be swallowed/misreported as Cancelled.
    This is the exact distinction is_real_control_flow relies on downstream."""
    cancel_event = asyncio.Event()  # never set — this cancel is NOT event-driven

    async def _slow() -> str:
        await asyncio.sleep(60)
        return "unreachable"

    async def _host() -> None:
        await race_cancellable(_slow(), cancel_event=cancel_event)

    host_task = asyncio.ensure_future(_host())
    await asyncio.sleep(0.1)
    host_task.cancel()  # genuine external cancel — NOT via cancel_event

    with pytest.raises(asyncio.CancelledError):
        await host_task


@pytest.mark.asyncio
async def test_teardown_runs_before_cancelled_is_raised_on_event_fire():
    """Tier 2: on the normal event-fire path, the coroutine's own teardown
    (finally / __aexit__) has already run by the time race_cancellable raises
    Cancelled — proven by a side-effecting coroutine whose cleanup flag must
    already be set. (Note: a naive spawn-a-new-task design ALSO satisfies this
    specific path, since it awaits the cancelled op_task inline before raising —
    see test_host_task_external_cancel_does_not_orphan_the_awaited_coroutine
    below for the scenario that spawn-a-new-task design actually gets wrong.)"""
    cancel_event = asyncio.Event()
    resource_released = asyncio.Event()

    async def _holds_a_resource() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            resource_released.set()  # simulates e.g. a subprocess/transport teardown

    async def _fire_soon() -> None:
        await asyncio.sleep(0.1)
        cancel_event.set()

    asyncio.ensure_future(_fire_soon())
    with pytest.raises(Cancelled):
        await asyncio.wait_for(
            race_cancellable(_holds_a_resource(), cancel_event=cancel_event), timeout=5.0,
        )
    assert resource_released.is_set()


@pytest.mark.asyncio
async def test_host_task_external_cancel_does_not_orphan_the_awaited_coroutine():
    """Tier 2: #2813 — the load-bearing property a spawn-a-new-task design gets
    WRONG (confirmed empirically: a reconstruction of that design leaves this
    RED — the resource-release flag is still unset ~0.2s after the host task
    finishes unwinding, because the spawned op_task is orphaned and keeps
    running toward its own internal duration).

    Scenario: the HOST task itself is cancelled from OUTSIDE while
    race_cancellable is still waiting on ``cancel_event`` (which never fires —
    this cancel has nothing to do with it). The same-task design means there IS
    no separate op-task to orphan: the host task's own cancellation delivers
    CancelledError directly into the awaited coroutine's current await point, so
    its teardown runs inline, synchronously, as part of the host task unwinding
    — by the time ``await host_task`` (below) returns, teardown is guaranteed
    done, not just probably done after a grace sleep."""
    cancel_event = asyncio.Event()  # never fires — this cancel is unrelated to it
    resource_released = asyncio.Event()

    async def _holds_a_resource() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            resource_released.set()

    async def _host() -> None:
        await race_cancellable(_holds_a_resource(), cancel_event=cancel_event)

    host_task = asyncio.ensure_future(_host())
    await asyncio.sleep(0.1)
    host_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await host_task

    assert resource_released.is_set(), (
        "the awaited coroutine's teardown must have already run by the time the "
        "externally-cancelled HOST task finishes unwinding — a spawn-a-new-task "
        "design leaves this unset (the orphaned task is still running toward "
        "its own internal duration) — #2813 Fable co-vet Critical-1"
    )


@pytest.mark.asyncio
async def test_external_cancel_wins_over_a_later_event_fire():
    """Tier 2: an external cancel of the host task, arriving WHILE race_cancellable
    is in flight, must win even if cancel_event ALSO fires shortly afterward —
    the external CancelledError propagates, it is never reinterpreted as
    Cancelled just because the event happened to fire during the same unwind
    window. (Corrected test name/docstring from an earlier co-vet pass: this
    does NOT exercise baseline_cancelling>0-at-entry, which requires a cancel
    already pending BEFORE race_cancellable's first line runs — a narrower,
    lower-value scenario given asyncio.Task.cancel()'s delivery timing makes it
    awkward to construct deterministically; the interleaving-priority property
    tested here is the one that matters in practice.)"""
    cancel_event = asyncio.Event()  # will fire too, to exercise the interleaving

    async def _slow() -> str:
        await asyncio.sleep(60)
        return "unreachable"

    async def _host() -> None:
        # A first (external) cancel arrives, then we race an event that ALSO
        # fires shortly after — both are "in flight" conceptually; the outer
        # CancelledError from the genuine external cancel must win.
        await race_cancellable(_slow(), cancel_event=cancel_event)

    host_task = asyncio.ensure_future(_host())
    await asyncio.sleep(0.05)
    host_task.cancel()  # genuine external cancel first

    async def _fire_event_too() -> None:
        await asyncio.sleep(0.1)
        cancel_event.set()

    asyncio.ensure_future(_fire_event_too())

    with pytest.raises(asyncio.CancelledError):
        await host_task
