"""Kill a test process that runs away with memory, and name the test that did it.

WHY THIS EXISTS
    On 2026-08-09 a single test reached ~10 GB and the operator's machine needed
    three reboots. The test collected frames from a producer whose length is
    decided by the *caller's* pace — bounded in the app, where a 10 fps timer
    calls it, and unbounded under ``list()``, which calls it as fast as the CPU
    allows. Nothing stopped it: the reviewer (me) had read the PR body rather
    than the test, and the machine has no ceiling of its own.

    A review question catches this the times someone asks it. A ceiling catches
    it every time, including the times nobody is looking — which is the case
    that produced the reboots.

WHY NOT AN OS LIMIT
    ``RLIMIT_AS`` and ``RLIMIT_DATA`` exist on macOS and cannot be lowered
    (measured: ``ValueError: current limit exceeds maximum limit`` against a hard
    limit of unlimited). Darwin does not enforce them. An external supervisor was
    the other option and was tried first — it watched the process and its direct
    children, missed the descendants, and let 10 GB through while set to stop at
    4 GB. Watching from inside needs no process tree at all.

WHAT IT BOUNDS
    One process. Under ``-n auto`` there is one ceiling per worker, so the
    machine-wide total is still the workers' sum — this stops a runaway, not a
    crowd. The 2026-08-09 runaway was a single process.

THE NUMBER
    2 GB by default. Measured on this tree: the whole suite's collection phase
    peaks at 382 MB in one process, and the heaviest single test file at 451 MB.
    Two gigabytes is four times the largest thing that legitimately happens and
    a fifth of what happened when something did not. Override with
    ``REYN_TEST_MEMORY_CEILING_MB`` when deliberately measuring something large.
"""
from __future__ import annotations

import os
import resource
import sys
import threading
import time

ENV_VAR = "REYN_TEST_MEMORY_CEILING_MB"
DEFAULT_CEILING_MB = 2048
EXIT_CODE = 97  # distinct from pytest's own 0-5, so a caller can tell them apart
_POLL_SECONDS = 0.25

# Darwin reports ru_maxrss in BYTES, Linux in KILOBYTES. Getting this wrong is
# silent in one direction (a ceiling 1024x too high never fires) which is the
# direction that looks fine.
_RSS_DIVISOR = 1048576 if sys.platform == "darwin" else 1024

_current_test = "<no test running>"
_config: object | None = None

# Under the repo root, not /tmp: a machine that reboots keeps this, and the
# next person looking for "what used the memory" looks in the repo.
LOG_PATH = os.path.join(os.getcwd(), ".reyn-memory-ceiling.log")


def _peak_mb() -> float:
    """This process's high-water RSS.

    ``ru_maxrss`` is a peak, not a current reading: it never falls. That is the
    right shape for a ceiling — a run that touched the limit and then freed is
    still a run that touched the limit — but it means this cannot be used to
    watch memory come back down.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / _RSS_DIVISOR


def ceiling_mb() -> int:
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return DEFAULT_CEILING_MB
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_CEILING_MB


def _watch(limit_mb: int) -> None:
    while True:
        peak = _peak_mb()
        if peak > limit_mb:
            # The test's name is the whole value here: "some pytest used 10 GB"
            # is where the 2026-08-09 investigation started, and it cost hours.
            message = (
                f"\n[reyn] MEMORY CEILING: this pytest process reached "
                f"{peak:.0f} MB (limit {limit_mb} MB) and was stopped.\n"
                f"[reyn] The test running at the time: {_current_test}\n"
                f"[reyn] Raise it deliberately with {ENV_VAR}=<mb> if this is "
                f"a measurement rather than a runaway.\n"
            )
            # The file FIRST, and unconditionally. pytest replaces fd 1 and 2
            # themselves, so both sys.stderr and a raw os.write(2, ...) land in a
            # capture buffer that os._exit discards — measured twice, each time
            # producing exit code 97 and an empty log. A file outlives the
            # process, which is the property the whole 2026-08-09 investigation
            # was missing: every runaway was gone before anyone could name it.
            try:
                with open(LOG_PATH, "a") as fh:
                    fh.write(message)
            except OSError:
                pass
            # Then the terminal, by asking pytest to stand aside. Best-effort:
            # if the capture manager is not there or refuses, the file already has it.
            try:
                cap = _config.pluginmanager.getplugin("capturemanager")  # type: ignore[union-attr]
                cap.suspend_global_capture(in_=True)
            except Exception:  # noqa: BLE001 — diagnostics must not raise on the way out
                pass
            try:
                os.write(2, message.encode())
            except OSError:
                pass
            os._exit(EXIT_CODE)
        time.sleep(_POLL_SECONDS)


def pytest_configure(config: object) -> None:
    global _config
    _config = config
    threading.Thread(
        target=_watch, args=(ceiling_mb(),), daemon=True, name="reyn-memory-ceiling"
    ).start()


def pytest_runtest_setup(item: object) -> None:
    """Record which test is running, so the ceiling can name it.

    Without this the message says a process died; with it, it says which test
    killed it. Every minute spent on 2026-08-09 between "python3.12 used 30 GB"
    and "this one test does" was spent recovering exactly this string.
    """
    global _current_test
    _current_test = getattr(item, "nodeid", str(item))
