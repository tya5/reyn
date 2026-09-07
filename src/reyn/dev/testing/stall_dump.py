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

WHY ALSO ARMED PER xdist WORKER (changed decision — #4986/#5909 tool ticket)
    This module originally armed the CONTROLLER only, on the reasoning that
    the observed hang was in the controller's own event loop
    (``_cancel_all_tasks`` → ``run_until_complete``) and arming per-worker
    would "multiply the timer for no additional signal." **That reason was
    falsified by a real incident** (architect finding, same-day #5909
    investigation): a controller stuck in ``queue.get()`` waiting on a
    silent WORKER is, by construction, invisible to a dump that only ever
    captures the controller's own threads — "the controller's dump is
    enough" was the premise the original reasoning depended on, and a hang
    genuinely INSIDE a worker's own session teardown is structurally
    outside what that premise covers. Per CLAUDE.md's rule that a
    describing comment is stale the moment the code it describes changes,
    this section (not just the code) moves with that finding.

    Each worker now arms its OWN watchdog too, into its OWN file
    (``.reyn-ci-stall-trace.<workerid>.log``, ``config.workerinput``'s own
    ``workerid`` — the same worker id ``reyn.dev.testing.worker_forensics``
    already keys its trace files by) at a threshold ``WORKER_OFFSET_SECONDS``
    BELOW the controller's own — e.g. controller 600s, worker 540s — so a
    run where both fire can be read in CAUSAL order: the worker's dump
    landing first says the hang started inside that worker, before the
    controller could have noticed anything wrong. A shared path written by
    several worker processes was deliberately avoided — faulthandler holds
    the fd it was armed with for the life of the arm (#5879, measured
    directly), so a shared path across processes is not "interleaved
    output", it is silent loss for whichever process's fd is not the one
    that ends up live.

THE NUMBER
    Must be comfortably under ``.github/workflows/test.yml``'s own outer
    ``timeout 12m`` (720s) — a dump that fires AFTER that kill never gets
    written, and comfortably ABOVE this suite's own normal completion time
    (~7-8 min measured on recent green runs) so a slow-but-healthy run
    never fires it. Set via ``REYN_STALL_TRACE_CI`` at the workflow level
    (not hardcoded here) so the margin can be re-tuned from one place if
    ``test.yml``'s own outer timeout ever changes. Each worker's own
    threshold is this SAME value minus :data:`WORKER_OFFSET_SECONDS` — one
    knob, not two, so re-tuning the workflow's env var re-tunes both.
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

#: See module docstring's "WHY ALSO ARMED PER xdist WORKER" — a worker's
#: own threshold is the controller's minus this, so a run where both fire
#: can be read in causal order (worker first = hang started in the worker).
WORKER_OFFSET_SECONDS = 60.0

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


def worker_log_path(workerid: str, root: "str | os.PathLike[str] | None" = None) -> str:
    """One file PER WORKER — never a path several worker processes share.
    See module docstring's "WHY ALSO ARMED PER xdist WORKER" for why a
    shared path is silent data loss here, not merely interleaved output."""
    return os.path.join(root if root is not None else os.getcwd(), f".reyn-ci-stall-trace.{workerid}.log")


def _worker_id(config: "pytest.Config") -> "str | None":
    workerinput = getattr(config, "workerinput", None)
    if not isinstance(workerinput, dict):
        return None
    workerid = workerinput.get("workerid")
    return str(workerid) if workerid else None


def worker_seconds_from(controller_seconds: float) -> float:
    """A worker's own threshold: the controller's own, minus
    :data:`WORKER_OFFSET_SECONDS`, clamped to a 1s floor (``arm()``'s own
    ``faulthandler.dump_traceback_later`` rejects a non-positive value).
    Pulled out as a pure function so the causal-ordering property (a
    worker's own dump fires BEFORE the controller's) is a fast, exact unit
    check rather than a race against real subprocess timing."""
    return max(controller_seconds - WORKER_OFFSET_SECONDS, 1.0)


def pytest_configure(config: "pytest.Config") -> None:
    seconds = _seconds_from_env()
    if seconds is None:
        return
    global _dump_file
    from reyn.runtime.stall_trace import arm

    if _is_xdist_worker(config):
        workerid = _worker_id(config)
        if workerid is None:
            return
        _dump_file = open(worker_log_path(workerid), "a", buffering=1)  # noqa: SIM115 — see module docstring
        arm(worker_seconds_from(seconds), file=_dump_file)
        return

    _dump_file = open(LOG_PATH, "a", buffering=1)  # noqa: SIM115 — see module docstring
    arm(seconds, file=_dump_file)
