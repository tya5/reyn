"""Tier 1: TrackedTaskSet's own contract (#4759).

#4759's root cause was a detached ``asyncio.create_task`` (SpawnTracker's
ephemeral-vanish task) invisible to ``AgentRegistry.shutdown()``'s drain,
orphaning a real MCP server subprocess. The fix is a single funnel every
background-task producer routes through (``TrackedTaskSet`` — see
``src/reyn/runtime/tracked_tasks.py``'s own module docstring), reached from
``AgentRegistry.shutdown()`` via ``Session.aclose_background_tasks()``. The
end-to-end structural witness for the ORIGINAL leak lives in
``tests/runtime/test_fp0063_arc_witness.py`` (real OS pid, real
``registry.shutdown()``). This file pins ``TrackedTaskSet`` itself — in
particular the re-entrancy hazard the fix's own design introduces and must
handle: the ephemeral-vanish task's real shape is "a tracked task that,
while running, itself calls ``aclose()`` on the SAME tracker" (SpawnTracker's
``_vanish_task`` runs ``registry.remove_session()``, which awaits
``Session.await_quiescent()``, which now calls
``self._background_tasks.aclose()`` — the currently-running task is still
tracked at that point, since its own done-callback hasn't fired yet).

These tests assert only the POSITIVE side (``aclose()`` returns; the tasks
it should complete/cancel actually do) — per testing policy (CLAUDE.md /
testing.md § Time), no test carries its own wait budget or time limit, so
there is deliberately no ``asyncio.wait_for``/timeout anywhere below; each
test awaits its condition unboundedly and relies on CI's own kill switch if
something is genuinely stuck. The NEGATIVE claim — that removing the
re-entrancy exclusion makes ``aclose()`` HANG rather than raise — was
verified by hand (strip the exclusion in ``tracked_tasks.py``, re-run this
file, observe the run not terminate; restore, re-run, observe green) and is
recorded in the PR body, not encoded as a test with its own timeout (a test
that starts a to-be-killed-by-a-timeout hang on every CI run for the rest of
this file's life is a worse defect than the one it would be demonstrating).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.runtime.tracked_tasks import TrackedTaskSet


@pytest.mark.asyncio
async def test_aclose_reentrant_from_a_task_it_is_itself_tracking_does_not_raise():
    """Tier 1: a tracked "await"-disposition task that itself calls
    ``aclose()`` on the SAME ``TrackedTaskSet`` it is tracked by (mirroring
    ``SpawnTracker._vanish_task``'s real call shape:
    ``remove_session -> await_quiescent -> background_tasks.aclose()``)
    returns normally, and the outer task itself later completes cleanly —
    the currently-running task is excluded from its OWN reentrant drain (see
    ``TrackedTaskSet.aclose``'s docstring for the precise guarantee this
    does and does not make)."""
    tracker = TrackedTaskSet()
    reentrant_call_completed = asyncio.Event()

    async def self_referencing_task() -> None:
        # At this point `task` (below) IS this coroutine's own task, and it
        # is STILL tracked (not done) — the exact shape a naive aclose()
        # would try to await itself for.
        await tracker.aclose()
        reentrant_call_completed.set()

    task = tracker.spawn(self_referencing_task(), disposition="await", name="self-referencing")

    await reentrant_call_completed.wait()
    await task
    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None


@pytest.mark.asyncio
async def test_reentrant_aclose_still_drains_a_different_tracked_task():
    """Tier 1: the re-entrancy exclusion is scoped to ``asyncio.
    current_task()`` only — a SIBLING tracked task (not the one currently
    calling ``aclose()``) must still be cancelled+joined by that same
    reentrant call. Otherwise the exclusion could silently widen into
    "aclose() does nothing while called reentrantly", which would defeat
    the whole point of draining everything else."""
    tracker = TrackedTaskSet()
    sibling_started = asyncio.Event()
    sibling_was_cancelled = False

    async def sibling() -> None:
        nonlocal sibling_was_cancelled
        sibling_started.set()
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            sibling_was_cancelled = True
            raise

    async def self_referencing_task() -> None:
        await sibling_started.wait()
        await tracker.aclose()

    tracker.spawn(sibling(), disposition="cancel_join", name="sibling")
    task = tracker.spawn(self_referencing_task(), disposition="await", name="self-referencing")

    await task
    assert sibling_was_cancelled


@pytest.mark.asyncio
async def test_aclose_cancels_cancel_join_and_leaves_await_disposition_to_finish():
    """Tier 1: the two dispositions behave differently under aclose() — a
    "cancel_join" task is cancelled; an "await" task is left to run to
    completion on its own (cancelling it would defeat tasks like the
    ephemeral-vanish task, whose entire job IS its own cleanup work)."""
    tracker = TrackedTaskSet()
    cancel_join_started = asyncio.Event()
    cancel_join_cancelled = False
    await_disposition_completed = False

    async def cancel_join_task() -> None:
        nonlocal cancel_join_cancelled
        cancel_join_started.set()
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancel_join_cancelled = True
            raise

    async def await_task() -> None:
        nonlocal await_disposition_completed
        await asyncio.sleep(0)
        await_disposition_completed = True

    tracker.spawn(cancel_join_task(), disposition="cancel_join", name="cancel-join-task")
    tracker.spawn(await_task(), disposition="await", name="await-task")
    # Let cancel_join_task actually start (reach its own sleep) before
    # aclose() cancels it -- cancelling a task before its coroutine has ever
    # run means its own except-block never executes at all, which would
    # make this test measure task.cancelled() instead of the task's own
    # observed cancellation, a different (weaker) claim.
    await cancel_join_started.wait()

    await tracker.aclose()

    assert cancel_join_cancelled
    assert await_disposition_completed


@pytest.mark.asyncio
async def test_aclose_on_an_empty_tracker_returns_immediately():
    """Tier 1: accept-side — aclose() on a tracker with nothing registered
    must not raise or block (the common case: most sessions never spawn a
    background task at all)."""
    tracker = TrackedTaskSet()
    await tracker.aclose()


@pytest.mark.asyncio
async def test_reentrant_aclose_logs_a_warning_naming_the_excluded_task(caplog):
    """Tier 1: the re-entrancy exclusion is not silent — lead-coder review
    (#4759): ``AgentRegistry.shutdown()``'s own call is EXPECTED to always
    be non-reentrant, but that expectation was not exhaustively traced
    across every ``shutdown()`` call site in the codebase. Rather than
    assert an unverified absolute, a reentrant exclusion always logs a
    WARNING naming the excluded task -- diagnostic (reentrancy is normal
    for the vanish task's own call), but a witness that would surface an
    unexpected reentrant call from a DIFFERENT caller if one ever occurs."""
    import logging

    tracker = TrackedTaskSet()
    reentrant_call_completed = asyncio.Event()

    async def self_referencing_task() -> None:
        await tracker.aclose()
        reentrant_call_completed.set()

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.tracked_tasks"):
        task = tracker.spawn(
            self_referencing_task(), disposition="await", name="self-referencing",
        )
        await reentrant_call_completed.wait()
        await task

    assert any(
        "reentrantly" in record.message and "self-referencing" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_non_reentrant_aclose_does_not_log_the_reentrancy_warning(caplog):
    """Tier 1: accept-side — an ordinary, non-reentrant aclose() call (the
    shape every real AgentRegistry.shutdown() call takes) must NOT log the
    reentrancy warning; otherwise the warning would be noise on every
    normal shutdown instead of a signal for the unexpected case."""
    import logging

    tracker = TrackedTaskSet()
    started = asyncio.Event()

    async def ordinary_task() -> None:
        started.set()
        await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.tracked_tasks"):
        tracker.spawn(ordinary_task(), disposition="cancel_join", name="ordinary-task")
        await started.wait()
        await tracker.aclose()  # called from the TEST's own task, not a tracked one

    assert not any("reentrantly" in record.message for record in caplog.records)
