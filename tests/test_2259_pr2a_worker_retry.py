"""Tier 2: #2259 PR-2a §4 — the DurabilityWorker durable-write RETRY (transient → escalate).

The worker's §4 failure contract (owner-decided): a TRANSIENT durable-write failure (an
``OSError`` — disk full / EIO / a momentary fs hiccup) is retried with bounded exponential
backoff; a PERSISTENT failure (retry-exhausted) is re-raised to the submitter = fail-stop
escalation. A non-``OSError`` (a programming error retrying cannot fix) is raised immediately.
The serial-FIFO ordering (enqueue order = durability order) holds even while a task is retrying
— a retry occupies the single drainer, so the next task waits.

Real DurabilityWorker (no mocks); plain async callables are the injected write tasks, with
small injected retry bounds + fast backoff for speed. The existing #1765 worker tests cover the
unchanged contract (FIFO / submit-awaits / loop-free / failure-surfaces) under the new
queue+drainer internals; this file covers the NEW retry behaviour.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from reyn.core.events.durability_worker import DurabilityWorker


def _fast_worker(max_attempts: int) -> DurabilityWorker:
    return DurabilityWorker(
        max_write_attempts=max_attempts, retry_base_s=0.001, retry_max_s=0.005,
    )


@pytest.mark.asyncio
async def test_transient_oserror_is_retried_then_succeeds():
    """Tier 2: a transient OSError is retried until the write succeeds — submit does NOT raise.
    RED if the worker did not retry (the first OSError would surface as a spurious failure)."""
    w = _fast_worker(max_attempts=4)
    attempts = {"n": 0}

    async def _flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("transient disk hiccup")
        # succeeds on the 3rd attempt

    await w.submit(_flaky)  # must NOT raise — the transient failure was retried away
    assert attempts["n"] == 3, "the transient OSError must be retried until it succeeds"
    await w.aclose()


@pytest.mark.asyncio
async def test_persistent_oserror_escalates_after_bounded_retries():
    """Tier 2: a PERSISTENT OSError (always failing) is retried a BOUNDED number of times, then
    re-raised to the submitter = fail-stop escalation. RED if it retried forever (no bound) or
    did not retry at all (escalated on the first failure)."""
    w = _fast_worker(max_attempts=5)
    attempts = {"n": 0}

    async def _always_fail() -> None:
        attempts["n"] += 1
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await w.submit(_always_fail)
    assert attempts["n"] == 5, "exactly max_write_attempts tries, THEN escalate (persistent)"
    await w.aclose()


@pytest.mark.asyncio
async def test_non_oserror_is_not_retried():
    """Tier 2: a non-OSError (a programming bug — retrying cannot help) is raised IMMEDIATELY,
    not retried. RED if the worker retried it (wasting the bounded budget on an unfixable bug)."""
    w = _fast_worker(max_attempts=5)
    attempts = {"n": 0}

    async def _bug() -> None:
        attempts["n"] += 1
        raise ValueError("a programming error")

    with pytest.raises(ValueError, match="a programming error"):
        await w.submit(_bug)
    assert attempts["n"] == 1, "a non-OSError must fail fast (single attempt, no retry)"
    await w.aclose()


@pytest.mark.asyncio
async def test_worker_survives_an_escalation_and_serves_the_next():
    """Tier 2: after a persistent failure escalates, the drainer keeps serving — the next task
    runs. RED if the escalation killed the drainer (a failed write would wedge all durability)."""
    w = _fast_worker(max_attempts=2)

    async def _always_fail() -> None:
        raise OSError("disk full")

    with pytest.raises(OSError):
        await w.submit(_always_fail)

    ran = False

    async def _ok() -> None:
        nonlocal ran
        ran = True

    await w.submit(_ok)  # the drainer survived the escalation
    assert ran is True
    await w.aclose()


@pytest.mark.asyncio
async def test_fifo_preserved_across_a_retrying_task():
    """Tier 2: serial-FIFO holds even while a task is retrying — the retry occupies the single
    drainer, so a later-submitted task runs only AFTER the retrying task completes. RED if a
    retry yielded the drainer to the next task (reordering durability)."""
    w = _fast_worker(max_attempts=4)
    order: list[str] = []
    first = {"n": 0}

    async def _retries_then_ok() -> None:
        first["n"] += 1
        if first["n"] < 3:
            raise OSError("hiccup")
        order.append("a")

    async def _b() -> None:
        order.append("b")

    ta = asyncio.create_task(w.submit(_retries_then_ok))
    await asyncio.sleep(0)  # let `a` enqueue first (FIFO setup)
    tb = asyncio.create_task(w.submit(_b))
    await asyncio.gather(ta, tb)
    assert order == ["a", "b"], "a retrying task still completes before the next (serial FIFO)"
    await w.aclose()


def test_loop_teardown_with_inflight_write_does_not_hang():
    """Tier 2: ``asyncio.run`` teardown (``_cancel_all_tasks``) must TERMINATE the background
    drainer even when it is cancelled MID-WRITE — the drainer must propagate ``CancelledError``,
    not swallow it + loop forever. This reproduces the full-suite hang (test_a2a_restart_resume's
    teardown hung in ``_cancel_all_tasks`` → ``select`` forever): a fire-and-forget submit leaves
    the drainer inside a still-running write when the loop tears down. Run in a daemon thread
    with a join timeout so a REGRESSION fails fast (assert) instead of hanging the suite."""
    def _run() -> None:
        async def _body() -> None:
            w = DurabilityWorker()

            async def _slow() -> None:
                await asyncio.sleep(30)  # still running when the loop tears down

            asyncio.ensure_future(w.submit(_slow))  # fire-and-forget → drainer goes mid-write
            await asyncio.sleep(0.05)               # let the drainer enter the write
            # return WITHOUT aclose → asyncio.run runs _cancel_all_tasks → must not hang

        asyncio.run(_body())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), (
        "event-loop teardown HUNG — the drainer swallowed CancelledError + looped on an empty "
        "queue, so _cancel_all_tasks never completed (the full-suite hang regression)"
    )
