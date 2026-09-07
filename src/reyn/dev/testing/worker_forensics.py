"""Name the killer of a silently-dead xdist worker (#5909).

WHY THIS EXISTS
    On 2026-09-07 four CI jobs (one on a docs-only PR) each lost an xdist
    worker — ``[gwN] node down: Not properly terminated`` — while running the
    same test, and the job log carried NOTHING else about that worker: no
    traceback, no ``Fatal Python error``, no memory-ceiling line. Everything a
    reader could reach for was either ambiguous or missing, and the issue's
    first hypothesis (a faulthandler dump into an execnet channel) had to be
    measured and set aside before anyone could say what it was NOT.

    What "nothing" excludes, and what it does not (measured, #5909):

    - pytest's own faulthandler plugin ``enable()``s on a ``dup`` of the
      ORIGINAL stderr at configure time (``_pytest/faulthandler.py``; global
      capture is SUSPENDED around ``pytest_configure``, so the dup is the
      real fd 2). Measured with CI's exact pins (pytest 9.1.1, xdist 3.8.0):
      a test that ``SIGSEGV``s itself inside an xdist worker under
      ``--capture=fd`` prints ``Fatal Python error: Segmentation fault`` and
      every thread's stack on the parent's stderr, and xdist then reports
      exactly the CI shape (``node down: Not properly terminated`` /
      ``worker crashed while running``). None of the four CI logs carried
      that line, so a fatal signal (SIGSEGV/SIGABRT/SIGBUS/SIGFPE/SIGILL)
      is out.
    - ``dev/testing/memory_ceiling.py`` writes its log only when REYN'S OWN
      watcher kills the worker (``os._exit(97)``). A host OOM kill
      (``SIGKILL``) writes nothing there — its silence does not exclude it.
    - pytest-timeout's ``thread`` method ends the worker with ``os._exit(1)``
      after printing its ``~~~ Stack of ... ~~~`` dump (which is NOT a
      faulthandler dump — same shape, different author) to the item's
      terminal writer — a worker's ``sys.stdout``, which execnet points at
      ``/dev/null``: silent in a worker. (The signal method, the default in
      a main thread, raises ``Failed`` inside the test instead and its dump
      DOES reach the job log — that is what the CI logs' ``~~~ Stack of``
      blocks are.)

    So the surviving candidates all end the worker without a Python-side
    record. The process's EXIT CODE tells them apart (``-9`` SIGKILL, ``1``
    ``os._exit(1)``, ``97`` reyn's ceiling) — and xdist has it (execnet's
    ``Popen``), but never prints it. Nor does ``-q`` say which tests that
    worker ran before dying, so "the same test is named 4/4 times" could be
    "that test" or "that point in a deterministic ``--dist load`` schedule".

WHAT THIS RECORDS (all under the repo cwd, like ``memory_ceiling``'s log)
    ``.reyn-worker-trace/<workerid>.log`` — one nodeid per line as each test
    STARTS in that worker (so a dead worker's last line is the test it died
    in, and the lines above are what ran before it), plus a final
    ``peak_rss_mb=<n>`` line at session end (``resource.ru_maxrss``, the same
    reader the memory ceiling uses).

    ``.reyn-worker-down.log`` — one line per worker that xdist reports
    DIED, written from ``pytest_testnodedown`` when (and only when) its
    ``error`` argument is not ``None``: worker id, xdist's own error text,
    the worker process's exit code (best-effort: execnet's private
    ``Popen`` handle, ``None`` if the shape ever changes), and the last few
    nodeids from that worker's trace. **Bug found and fixed by #5934**
    (lead-coder review): an earlier version of this hook wrote a line on
    EVERY call, not only crash calls — xdist's own ``dsession.py`` calls
    this same hook, with ``error=None``, from ``worker_workerfinished``,
    the ORDINARY per-worker completion path — so every worker in an
    ``-n auto``/``-n N`` run wrote a line here at the end of EVERY run,
    whether or not anything went wrong, and the CI step's own
    ``[ -s .reyn-worker-down.log ]`` "did anything die" check fired its
    ``::warning::`` on every single run as a result (confirmed directly:
    3 real CI job logs, one of them fully green, all showing 4 lines,
    ``error=None`` every time). ``error is None`` is exactly xdist's own
    discriminator between the two call sites (``worker_workerfinished``
    vs. ``worker_errordown`` — confirmed by reading ``dsession.py``
    itself, not inferred), so the hook now skips the write on that path —
    this file is now genuinely non-empty only when a worker actually died.

    ``.github/workflows/test.yml`` prints both after every run (with the
    host's ``dmesg`` OOM lines and ``free -m``/``nproc`` before), so the next
    crash names its killer in the job log instead of costing another
    investigation round-trip. The per-worker trace files are written
    incrementally as each test STARTS, independent of the controller, so
    they are the only test-order evidence that survives a run where the
    WHOLE JOB dies before the controller can even reach its own
    ``pytest_testnodedown`` hook. The workflow prints each worker's FULL
    trace (part of #5909), not only ``peak_rss_mb=``, and also uploads
    ``.reyn-worker-trace/`` and ``.reyn-worker-down.log`` as a build
    artifact — a run's job log is truncated past GitHub's own size
    ceiling, and the artifact survives that and stays fetchable long after
    the run without re-triggering CI. (An earlier version of this
    docstring claimed three same-day #5909 recurrences — #5926, #5916,
    #5931 — left ``.reyn-worker-down.log`` EMPTY, offered as this feature's
    own motivating measurement. That claim was checked against the wrong
    evidence and was false: all three jobs' logs show 4 lines, all
    ``error=None returncode=None`` — the ordinary-completion shape above,
    not evidence of a crash at all. See #5934's PR discussion for the
    corrected reading of those three incidents.)

COST
    One small append per test start (open/append/close — no held fd, so
    nothing here can be the #5877 fd-reuse hazard it helps diagnose). Off
    on a non-xdist run (no ``workerinput`` → no worker id → nothing written).
"""
from __future__ import annotations

import os
from pathlib import Path

TRACE_DIR = ".reyn-worker-trace"
DOWN_LOG = ".reyn-worker-down.log"
#: How many of the dead worker's most recent tests the down-log line quotes.
RECENT_TESTS = 5


def worker_id(config: object) -> "str | None":
    """xdist's worker id (``gw0`` ...) inside a worker, ``None`` on the
    controller or a plain serial run — ``config.workerinput`` is xdist's own
    discriminator (present only in a worker subprocess), the same one
    ``stall_dump.py`` already reads."""
    workerinput = getattr(config, "workerinput", None)
    if not isinstance(workerinput, dict):
        return None
    wid = workerinput.get("workerid")
    return str(wid) if wid else None


def trace_path(wid: str, root: "str | os.PathLike[str] | None" = None) -> Path:
    return Path(root if root is not None else os.getcwd()) / TRACE_DIR / f"{wid}.log"


def _append(path: Path, line: str) -> None:
    """Best-effort append — a forensics writer that can fail the run it
    watches is worse than none (same policy as ``memory_ceiling``'s log)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def record_test_start(wid: str, nodeid: str, root: "str | os.PathLike[str] | None" = None) -> None:
    _append(trace_path(wid, root), nodeid)


def record_peak_rss(wid: str, peak_mb: float, root: "str | os.PathLike[str] | None" = None) -> None:
    _append(trace_path(wid, root), f"peak_rss_mb={peak_mb:.0f}")


def recent_tests(wid: str, root: "str | os.PathLike[str] | None" = None, n: int = RECENT_TESTS) -> "list[str]":
    """The last *n* nodeids in *wid*'s trace (the ``peak_rss_mb=`` line, if
    the worker got as far as writing one, is not a test and is skipped)."""
    try:
        lines = trace_path(wid, root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    tests = [ln for ln in lines if ln and not ln.startswith("peak_rss_mb=")]
    return tests[-n:]


def format_node_down(wid: str, error: object, returncode: "int | None", recent: "list[str]") -> str:
    """The one line a reader needs: who died, what xdist said, how the
    process ended, and what it was doing. ``returncode`` is the process's
    own exit status: a negative number is the signal that killed it
    (``-9`` = SIGKILL — the host's OOM killer or a reaper, never reyn),
    ``1`` is pytest-timeout's thread-method ``os._exit(1)``, ``97`` is
    ``memory_ceiling``'s own kill."""
    signal_note = ""
    if isinstance(returncode, int) and returncode < 0:
        signal_note = f" (signal {-returncode}{' SIGKILL' if -returncode == 9 else ''})"
    last = recent[-1] if recent else "<no test recorded>"
    before = recent[:-1]
    return (
        f"worker={wid} error={error!r} returncode={returncode!r}{signal_note} "
        f"last_test={last} before_it={before!r}"
    )


def returncode_of(node: object) -> "int | None":
    """The dead worker process's exit status, read through execnet's own
    ``Popen`` handle (``node.gateway._io.popen`` — a private shape; every
    step is ``getattr``-guarded so a future execnet that moves it yields
    ``None`` here, never an exception in a hook that runs in execnet's
    receiver thread)."""
    popen = getattr(getattr(getattr(node, "gateway", None), "_io", None), "popen", None)
    if popen is None:
        return None
    try:
        rc = popen.poll()
    except Exception:  # noqa: BLE001 — forensics never raise
        return None
    return rc if isinstance(rc, int) else None


# ── pytest / xdist hook bodies (called from tests/conftest.py) ───────────────


def pytest_runtest_setup(item: object) -> None:
    """Worker: record the test that is about to run."""
    wid = worker_id(getattr(item, "config", None))
    if wid is not None:
        record_test_start(wid, str(getattr(item, "nodeid", "?")))


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    """Worker: record this worker's peak RSS — the figure a host OOM kill
    would have been judging, which ``memory_ceiling``'s own log never shows
    for a worker that stayed under reyn's ceiling."""
    wid = worker_id(getattr(session, "config", None))
    if wid is None:
        return
    from reyn.dev.testing.memory_ceiling import peak_mb

    record_peak_rss(wid, peak_mb())


def pytest_testnodedown(node: object, error: object) -> None:
    """Controller: xdist calls this hook on BOTH a worker's ordinary
    completion AND a genuine crash (``dsession.py``'s own
    ``worker_workerfinished`` — the ordinary path — calls it with
    ``error=None``; only ``worker_errordown`` — the crash path — calls it
    with a real error object). Bug found and fixed by this same PR
    (#5934, lead-coder review): an earlier version of this hook wrote a
    line for EVERY call, so ``.reyn-worker-down.log`` had one line per
    worker on every run, ALWAYS non-empty regardless of whether anything
    went wrong — verified directly against 3 real CI runs (one fully
    green) all showing 4 lines, ``error=None`` every time, and the CI
    step's own ``[ -s .reyn-worker-down.log ]`` check firing its
    ``::warning::`` on every single run as a result. ``error is None`` is
    exactly xdist's own discriminator for "this was ordinary completion,
    not a crash" — skip the write there; a normal worker's own recent
    tests are already visible in its own trace file if anyone wants them,
    with no need to duplicate them into the crash log."""
    if error is None:
        return
    wid = str(getattr(getattr(node, "gateway", None), "id", "?"))
    _append(
        Path(os.getcwd()) / DOWN_LOG,
        format_node_down(wid, error, returncode_of(node), recent_tests(wid)),
    )
