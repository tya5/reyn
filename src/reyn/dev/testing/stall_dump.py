"""CI teardown-hang diagnostic (#4986): dump every thread's stack if the
pytest session itself does not finish within ``REYN_STALL_TRACE_CI`` seconds.

WHY THIS EXISTS
    #4986: CI's own pytest run has hung 3 times in session TEARDOWN (asyncio's
    ``_cancel_all_tasks`` stuck in ``gather()``), each time with ZERO
    diagnostic surviving — confirmed structurally, not by guesswork: both
    pytest-timeout's ``--timeout`` and pytest's own builtin
    ``faulthandler_timeout`` wrap ONLY ``pytest_runtest_protocol`` per item
    (``_pytest/faulthandler.py``'s own source), cancelling the watchdog the
    instant the LAST test's protocol returns — neither can ever see a hang
    that happens strictly AFTER that point. This arms a SEPARATE,
    session-spanning watchdog (reusing ``reyn.runtime.stall_trace``'s
    ``arm``, #4405 — the same ``faulthandler.dump_traceback_later``
    primitive, not a new mechanism).

WHY THIS NEVER DISARMS (architect finding, PR #5362 review,
issuecomment-5445125316)
    The first version of this module cancelled the timer at
    ``pytest_sessionfinish`` — but ``pytest_sessionfinish`` returning is
    NOT the end of the process: interpreter shutdown, ``atexit`` handlers,
    and non-daemon thread joins all still remain, and THAT is a class of
    hang this repo has actually hit for real (PR #5049's
    ``ThreadedTransportProxy``: an assert failure → an ``Event`` never set
    → a non-daemon thread left running → ``atexit``'s own thread-join
    hangs). Disarming at ``pytest_sessionfinish`` would have structurally
    excluded the exact failure class #4986 exists to catch. Fixed by never
    disarming at all: ``arm()`` is called with ``exit=False``, so a timer
    that outlives the test session merely dumps (repeatedly, every
    ``REYN_STALL_TRACE_CI`` seconds) and does nothing else — a HEALTHY
    process has already exited (ending the background thread with it)
    long before the threshold arrives, so this costs nothing on a green
    run; a process still alive at the threshold is, by construction,
    already taking longer than this suite's own normal completion time by
    a wide margin — exactly the condition worth dumping for, whether the
    hang is inside pytest's own session or in the interpreter's shutdown
    sequence afterward.

WHY GATED, NOT ALWAYS ON
    Opt-in via ``REYN_STALL_TRACE_CI`` (seconds) — unset means this file does
    nothing, zero behavior change for local/dev runs. On a healthy CI run,
    the WHOLE PROCESS exits (ending the background timer with it) well
    before the threshold would ever be reached (normal completion is ~7-8
    minutes; :data:`LOG_PATH`'s own workflow value leaves comfortable
    margin), so a green run gains nothing from this beyond one empty,
    unwritten file (faulthandler needs an already-open file object at ARM
    time, so opening happens regardless of whether the timer ever fires —
    the CI step that surfaces this file tests for NON-EMPTY content, not
    mere existence, for exactly this reason) — no added cost worth naming,
    no added output. Dump CONTENT only appears when something has already
    gone wrong (CLAUDE.md band: cost/budget) — see "WHY THIS NEVER
    DISARMS" above for why process-exit, not a cancel call, is what ends
    this on the healthy path.

WHY A DISK FILE, NOT sys.stderr
    ``faulthandler.dump_traceback_later``'s destination is fixed at ARM
    time — there is no chance to redirect it once teardown is already
    hanging. pytest's own capture manager can own ``sys.stderr`` for parts
    of a session (the same hazard ``memory_ceiling.py`` already solved for
    its own kill message — see that module's ``LOG_PATH`` comment). A
    plain, already-open disk file sidesteps that entirely: writing to a
    real fd outside pytest's capture is unaffected by whatever pytest does
    to the process's own stdout/stderr streams.

WHY NOT ARMED IN EVERY xdist WORKER
    The observed hang is in the CONTROLLER's own event loop
    (``_cancel_all_tasks`` → ``run_until_complete``, per the #4986 stack
    dumps already on file) — ``config.workerinput`` is xdist's own
    controller/worker discriminator (present only inside a worker
    subprocess). Arming per-worker would multiply the timer for no
    additional signal and, on the rare occasion it DOES fire, multiply the
    dump output N-workers-over for nothing.

THE NUMBER
    Must be comfortably under ``.github/workflows/test.yml``'s own outer
    ``timeout 12m`` (720s) — a dump that fires AFTER that kill never gets
    written, and comfortably ABOVE this suite's own normal completion time
    (~7-8 min measured on recent green runs) so a slow-but-healthy run
    never fires it. Set via ``REYN_STALL_TRACE_CI`` at the workflow level
    (not hardcoded here) so the margin can be re-tuned from one place if
    ``test.yml``'s own outer timeout ever changes.
"""
from __future__ import annotations

import os
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_ENV_VAR = "REYN_STALL_TRACE_CI"

#: Under the repo root (matches ``memory_ceiling.py``'s own ``LOG_PATH``
#: convention), not a scratch dir that a CI runner discards before anyone
#: could read it, and not ``.reyn/`` (a real project's own dir, which a
#: bare `pytest` invocation from a fresh checkout may not even have yet).
LOG_PATH = os.path.join(os.getcwd(), ".reyn-ci-stall-trace.log")

#: Kept open for the whole session (faulthandler needs an already-open
#: file object at ARM time, and may write to it from a background thread
#: at any later moment) — closed implicitly at process exit; there is no
#: earlier safe point to close it without risking the very hang this
#: module exists to catch.
_dump_file: "IO[str] | None" = None


def _seconds_from_env() -> "float | None":
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _is_xdist_worker(config: "pytest.Config") -> bool:
    return hasattr(config, "workerinput")


def pytest_configure(config: "pytest.Config") -> None:
    if _is_xdist_worker(config):
        return
    seconds = _seconds_from_env()
    if seconds is None:
        return
    global _dump_file
    from reyn.runtime.stall_trace import arm

    _dump_file = open(LOG_PATH, "a", buffering=1)  # noqa: SIM115 — see module docstring
    arm(seconds, file=_dump_file)
