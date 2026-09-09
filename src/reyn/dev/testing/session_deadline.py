"""Graceful session-wide deadline (#5994 stage ①): stop collecting further
test items once the session has run for ``REYN_TEST_SESSION_DEADLINE_S``,
so a run that hits its own outer ``timeout 12m`` (``.github/workflows/
test.yml``) reports a normal summary and names where it stopped -- instead
of being killed by that external ``timeout`` with zero pytest output
surviving.

WHY THIS EXISTS (#5994's own measurement)
    24 recent main-push runs of ``pytest (Python 3.12)`` ranged 9.98-12.90
    minutes (mean 11.69), riding hard against the outer ``timeout 12m`` --
    not a rare tail event but the shape of the whole distribution. 2 of
    those 24 hit exactly this: exit 124, zero summary line, "which test"
    and "how many passed" both unanswerable from the log. Raising the
    timeout was explicitly rejected (lead-coder ruling) -- the fix is what
    survives hitting the SAME deadline, not a later one (CLAUDE.md band
    Q3: does the repair destroy the evidence?).

WHY ``session.shouldstop``, not a new kill mechanism
    pytest's own documented, built-in graceful-stop primitive
    (``_pytest/main.py``'s ``pytest_runtestloop`` checks
    ``session.shouldstop`` after EVERY item -- the SAME primitive
    ``-x``/``--maxfail`` already use) exists for exactly this shape.
    Setting it to a non-empty string lets the CURRENTLY-running item
    finish and report normally, THEN stops collecting more, THEN prints
    pytest's own summary -- nothing here forces a test mid-execution to
    abort.

WHY ARMED PER xdist WORKER, not the controller
    Under ``-n auto`` the controller (``xdist/dsession.py``) never runs a
    test itself -- each WORKER runs its own independent ``pytest.Session``,
    executing whatever subset the controller dispatches. Setting
    ``shouldstop`` on the controller's own (test-less) session would do
    nothing observable; each worker independently decides to stop, and the
    controller aggregates whatever reports it already received into the
    normal summary line, same as any other early stop. This module arms
    in EVERY process this conftest loads into (controller included) --
    the controller's own session simply never has anything to stop.

WHY GATED, NOT ALWAYS ON
    Opt-in via ``REYN_TEST_SESSION_DEADLINE_S`` (seconds) -- unset means
    this file arms nothing, zero behavior change for a local run. Set at
    the workflow level (``test.yml``), DERIVED from the same ``timeout
    <N>m`` value the outer ``timeout`` command already uses in that same
    shell step -- never a second, independently-chosen number (see that
    file's own comment for the arithmetic and why: two related numbers
    living in two places is exactly what lets one move without the
    other).

THE MARGIN
    ``shouldstop`` is only ever CHECKED between items, so the item running
    exactly when the deadline fires can still take up to its own
    ``--timeout=<N>`` ceiling to finish afterward. ``test.yml``'s own
    arithmetic budgets the deadline comfortably below the outer kill to
    cover that plus report-generation time plus a safety margin -- not
    restated here so this docstring cannot go stale if that arithmetic is
    retuned.

WHY A DEADLINE <= 0 STOPS SYNCHRONOUSLY, NOT VIA A TIMER
    ``threading.Timer(0, ...)`` (or a negative delay) still runs its
    callback on a SEPARATE thread pytest never waits for -- nothing
    guarantees that thread is scheduled before collection starts, so a
    caller passing an already-past deadline (test.yml's own arithmetic
    computing <= 0, a genuine possibility if the outer budget shrinks) could
    still race collection into running items it should never reach. An
    already-elapsed deadline needs no thread at all: this sets
    ``session.shouldstop`` directly, in the same call, before any item is
    collected -- deterministic, not a race.
"""
from __future__ import annotations

import os
import threading

import pytest

_ENV = "REYN_TEST_SESSION_DEADLINE_S"


def pytest_sessionstart(session: pytest.Session) -> None:
    raw = os.environ.get(_ENV)
    if not raw:
        return  # unset -> armed nowhere, zero behavior change (see docstring)
    deadline_s = float(raw)

    def _stop() -> None:
        # A plain attribute write -- pytest's own runtestloop reads this
        # after every item's protocol returns. Safe from a background
        # thread: no lock needed, no pytest API called off the main thread.
        session.shouldstop = (
            f"#5994: session-wide deadline ({deadline_s:.0f}s) reached -- "
            "stopping to report a summary before the external CI timeout"
        )

    if deadline_s <= 0:
        _stop()  # already past -- no thread, no race (see docstring)
        return

    timer = threading.Timer(deadline_s, _stop)
    timer.daemon = True  # never blocks interpreter shutdown on its own
    timer.start()
