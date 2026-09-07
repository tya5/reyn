"""Tier 1: Contract — reyn.dev.testing.stall_dump, the #4986 CI
teardown-hang diagnostic.

Same style as tests/dev/test_extra_skip_report_4104.py: a REAL, isolated
inner pytest session (via pytester's own subprocess seam) is the only way
to exercise a genuine pytest_configure without faking pytest's own session
lifecycle. Run as real SUBPROCESSES (not pytester's in-process runpytest())
so the plugin's own faulthandler ``arm()`` call never interacts with THIS
outer test session's own faulthandler state.

stall_dump deliberately has no pytest_sessionfinish hook (architect
finding, PR #5362 review) — the watchdog is never cancelled; a healthy
process's own exit ends it. See that module's own "WHY THIS NEVER DISARMS"
docstring section.

No duration anywhere the assertion depends on: the inner session's own
hang is a REAL, deterministic block (``sys.stdin.readline()`` on a pipe
this test controls, released by closing it — the same idiom this
repo's own real-subprocess tests already use), and the outer wait for the
dump file to appear is an unbounded poll (``while not condition: sleep(0)``)
— the ceiling is whatever timeout wraps THIS test itself, never a
self-authored one. ``REYN_STALL_TRACE_CI``'s own seconds value is the
injected clock CLAUDE.md's duration rule asks for when a duration
genuinely is the subject, not a guessed wait.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_INNER_CONFTEST = 'pytest_plugins = ["reyn.dev.testing.stall_dump"]\n'

_INNER_TEST_HANGS_AT_TEARDOWN = """
import sys
from pathlib import Path
import pytest

@pytest.fixture(scope="session", autouse=True)
def _hang_forever_at_teardown():
    yield
    # Blocks until the outer test closes this process's stdin — a real,
    # deterministic wait, released deterministically, never a sleep.
    sys.stdin.readline()

def test_body():
    # A durable, unbuffered marker the OUTER test polls for — proof the
    # test body itself already ran and only the deliberate teardown hang
    # remains (pytest's own final summary line never prints while that
    # hang holds the session open, so stdout/stderr can't serve this role).
    Path("test_started.marker").write_text("started")
    assert True
"""

_INNER_TEST_NORMAL = "def test_body():\n    assert True\n"

_INNER_TEST_WORKER_HANGS_AT_TEARDOWN = """
from pathlib import Path
import pytest

@pytest.fixture(scope="session", autouse=True)
def _hang_forever_at_teardown():
    yield
    # A FIFO, not stdin: see the outer test's own docstring for why —
    # execnet remaps a worker's sys.stdin, so the controller-hang tests'
    # own readline() idiom does not reach into a worker.
    with open("release.fifo") as f:
        f.read()

def test_body():
    Path("test_started.marker").write_text("started")
    assert True
"""

_INNER_TEST_HANGS_AT_ATEXIT = """
import atexit
import sys

def _hang_forever():
    sys.stdin.readline()

atexit.register(_hang_forever)

def test_body():
    assert True
"""


def test_an_atexit_hang_after_sessionfinish_also_produces_a_stall_dump(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: the specific class architect's PR #5362 review named — a
    hang in interpreter shutdown/``atexit``, strictly AFTER
    ``pytest_sessionfinish`` has already returned (this repo's own real
    precedent: PR #5049's ``ThreadedTransportProxy``, a non-daemon thread
    left running past session end, joined at ``atexit``) — is still
    caught. This is the exact case a `pytest_sessionfinish`-cancelled
    timer would have missed; stall_dump deliberately has no such hook
    (see its own module docstring's "WHY THIS NEVER DISARMS")."""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TEST_HANGS_AT_ATEXIT)
    monkeypatch.setenv("REYN_STALL_TRACE_CI", "1")

    log_path = Path(pytester.path) / ".reyn-ci-stall-trace.log"
    proc = pytester.popen(
        [sys.executable, "-m", "pytest", "-q", "-s", "test_inner.py"],
        stdin=subprocess.PIPE,
    )
    try:
        # #5394: faulthandler writes its dump in stages — the
        # "Timeout (...)!" header, then a "Thread ... (most recent call
        # first):" line, then the actual stack frames ("File \"...\",
        # line N in func"). Waiting on mere non-emptiness (or even on
        # "Thread" alone, measured directly while building this fix —
        # that string is written BEFORE the frames) reads the file mid-
        # write, racing the LAST part (machine-speed dependent — the same
        # tree can go green or red). Wait on the actual completion this
        # test needs instead: content containing a real stack frame IS
        # "the dump finished writing", not a duration to guess at.
        content = ""
        while "File \"" not in content:
            if log_path.exists():
                content = log_path.read_text()
            if "File \"" not in content:
                time.sleep(0)
    finally:
        assert proc.stdin is not None
        proc.stdin.close()  # releases _hang_forever's readline()
        proc.wait()

    # lead-coder TESTS-READ (non-blocking, #5394): the wait condition
    # above (`"File \"" in content`) ALREADY implies `"Thread" in
    # content` — faulthandler writes header -> "Thread ..." -> "File
    # ...", in that order, so once a frame line has landed the thread
    # line is guaranteed present too. Asserting that again would never
    # fail (a silent tautology after this fix — a genuine future partial-
    # dump bug would now hang to CI's own 120s timeout instead of this
    # test's own readable message). Assert the ORDER instead — a
    # property the wait condition does NOT imply (content merely
    # containing all three substrings says nothing about their
    # sequence).
    header_at = content.find("Timeout")
    thread_at = content.find("Thread")
    frame_at = content.find("File \"")
    assert -1 < header_at < thread_at < frame_at, (
        f"the dump should write its header, then the thread line, then "
        f"stack frames, in that order — got header@{header_at}, "
        f"thread@{thread_at}, frame@{frame_at} in {content!r}"
    )


def test_a_session_teardown_hang_produces_a_stall_dump(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: the mechanism this issue exists for actually fires. A session
    whose own teardown blocks forever (the #4986 shape: something after the
    last test's own result is already decided never lets the process exit)
    produces a non-empty stall-trace dump file — not merely "no crash", the
    file must contain a real thread stack.

    Strip-falsifier: unset REYN_STALL_TRACE_CI (or revert
    reyn/dev/testing/stall_dump.py's pytest_configure to a no-op) and this
    goes red — the log file never appears, because nothing armed the
    watchdog."""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TEST_HANGS_AT_TEARDOWN)
    monkeypatch.setenv("REYN_STALL_TRACE_CI", "1")

    log_path = Path(pytester.path) / ".reyn-ci-stall-trace.log"
    assert not log_path.exists(), "test setup invariant: no stale dump file"

    # `-s`: pytest's own capture manager replaces sys.stdin with an object
    # that RAISES on read rather than blocking (`DontReadFromInput`) —
    # without this flag the fixture's `readline()` never actually blocks,
    # it errors out immediately and the session finishes right away.
    proc = pytester.popen(
        [sys.executable, "-m", "pytest", "-q", "-s", "test_inner.py"],
        stdin=subprocess.PIPE,
    )
    try:
        # #5394: faulthandler writes its dump in stages — the
        # "Timeout (...)!" header, then a "Thread ... (most recent call
        # first):" line, then the actual stack frames ("File \"...\",
        # line N in func"). Waiting on mere non-emptiness (or even on
        # "Thread" alone, measured directly while building this fix —
        # that string is written BEFORE the frames) reads the file mid-
        # write, racing the LAST part (machine-speed dependent — the same
        # tree can go green or red; a header-only read was measured
        # directly in a real CI red, #5394). Wait on the actual
        # completion this test needs instead: content containing a real
        # stack frame IS "the dump finished writing", not a duration to
        # guess at.
        content = ""
        while "File \"" not in content:
            if log_path.exists():
                content = log_path.read_text()
            if "File \"" not in content:
                time.sleep(0)
    finally:
        assert proc.stdin is not None
        proc.stdin.close()  # releases _hang_forever_at_teardown's readline()
        proc.wait()

    # lead-coder TESTS-READ (non-blocking, #5394): see the sibling test's
    # own comment on why `"Thread" in content` is a tautology here (the
    # wait condition already implies it) and why the order check below
    # is the property actually worth asserting.
    header_at = content.find("Timeout")
    thread_at = content.find("Thread")
    frame_at = content.find("File \"")
    assert -1 < header_at < thread_at < frame_at, (
        f"the dump should write its header, then the thread line, then "
        f"stack frames, in that order — got header@{header_at}, "
        f"thread@{thread_at}, frame@{frame_at} in {content!r}"
    )


def test_a_normal_session_produces_no_stall_dump(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: regression guard — a session that finishes normally, well
    inside the configured threshold, must not produce dump CONTENT. Mirrors
    #4986's own "no cost on a green run" requirement: stall_dump never
    disarms (see its own module docstring) — what ends the timer on a
    healthy run is the PROCESS ITSELF exiting long before the threshold
    arrives, not a cancel call.

    (The log file itself is opened, empty, the moment the watchdog is
    armed — faulthandler needs an already-open file object, so "opened"
    and "written to" are different claims; the one that matters for cost
    is the latter.)"""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TEST_NORMAL)
    monkeypatch.setenv("REYN_STALL_TRACE_CI", "600")

    result = pytester.runpytest_subprocess("test_inner.py")
    result.assert_outcomes(passed=1)

    log_path = Path(pytester.path) / ".reyn-ci-stall-trace.log"
    assert not (log_path.exists() and log_path.stat().st_size > 0), (
        "#4986 REGRESSION: a normal, fast session must not leave a "
        "stall-trace DUMP (non-empty content) behind"
    )


def test_an_xdist_worker_teardown_hang_produces_its_own_worker_dump(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: #4986/#5909 tool ticket — architect's finding that a hang
    strictly INSIDE an xdist worker's own session teardown is invisible to
    the CONTROLLER's own dump (the controller is idle in ``queue.get()``,
    nothing of its own is stuck). ``stall_dump.py`` now arms a SEPARATE
    watchdog inside the worker itself, writing to the worker's OWN file
    (``worker_log_path``) rather than the controller's ``LOG_PATH`` — this
    is the property that changed decision (see module docstring's "WHY
    ALSO ARMED PER xdist WORKER").

    Strip witness: reverting ``pytest_configure``'s worker branch to the
    module's original early ``return`` on ``_is_xdist_worker(config)``
    leaves NO file under ``.reyn-ci-stall-trace.gw0.log`` here — verified
    directly, restored after.

    No duration anywhere the assertion depends on: the inner worker's
    teardown hang is a real, deterministic block on a FIFO ``open()``/
    ``read()``, released by opening its write end and closing it; the
    outer wait for the worker's dump file is an unbounded poll. A FIFO
    rather than ``sys.stdin`` (the sibling controller-hang tests' own
    idiom): measured directly while building this test — execnet remaps
    an xdist WORKER's own ``sys.stdin``, so ``readline()`` inside a
    worker returns immediately instead of blocking; a FIFO blocks at the
    kernel level regardless of what execnet did to this process's stdio."""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TEST_WORKER_HANGS_AT_TEARDOWN)
    monkeypatch.setenv("REYN_STALL_TRACE_CI", "1")

    fifo_path = Path(pytester.path) / "release.fifo"
    os.mkfifo(fifo_path)

    worker_log = Path(pytester.path) / ".reyn-ci-stall-trace.gw0.log"
    assert not worker_log.exists(), "test setup invariant: no stale worker dump file"

    proc = pytester.popen(
        [sys.executable, "-m", "pytest", "-q", "-s", "-n", "1", "test_inner.py"],
    )
    try:
        # Same staged-write wait as the sibling tests: a real stack frame
        # line is "the dump finished writing", not a guessed duration.
        content = ""
        while "File \"" not in content:
            if worker_log.exists():
                content = worker_log.read_text()
            if "File \"" not in content:
                time.sleep(0)
    finally:
        # Releases the worker's blocked FIFO ``open()``: opening the write
        # end lets the read-side ``open()`` return, and closing it
        # immediately hands the worker's own ``read()`` an EOF.
        write_fd = os.open(str(fifo_path), os.O_WRONLY)
        os.close(write_fd)
        proc.wait()

    header_at = content.find("Timeout")
    thread_at = content.find("Thread")
    frame_at = content.find("File \"")
    assert -1 < header_at < thread_at < frame_at, (
        f"the worker's own dump should write its header, then the thread "
        f"line, then stack frames, in that order — got header@{header_at}, "
        f"thread@{thread_at}, frame@{frame_at} in {content!r}"
    )
    # NOTE: this does NOT assert the controller's own dump stays empty —
    # with a hanging worker, the controller's own session also never
    # finishes (it is waiting on that worker), so its own watchdog fires
    # too; that is expected, not a shared-timer bug. See
    # test_worker_seconds_from_stays_below_the_controllers_own_threshold
    # for the actual causal-ordering property (a unit check on the exact
    # offset arithmetic, not a real-time race between the two).


def test_worker_seconds_from_stays_below_the_controllers_own_threshold() -> None:
    """Tier 1: Contract — ``worker_seconds_from`` (used by
    ``pytest_configure``'s worker branch) always returns a value strictly
    below the controller's own configured seconds, clamped to a 1s floor.
    This is the causal-ordering property module docstring's "WHY ALSO
    ARMED PER xdist WORKER" names (a worker's own dump should fire before
    the controller's, so a run where both fire reads in causal order) —
    checked exactly, as a pure function, rather than raced against real
    subprocess timing (which the integration test above deliberately does
    NOT attempt, for exactly this reason)."""
    from reyn.dev.testing.stall_dump import WORKER_OFFSET_SECONDS, worker_seconds_from

    assert worker_seconds_from(600.0) == 600.0 - WORKER_OFFSET_SECONDS
    assert worker_seconds_from(600.0) < 600.0
    # Floored at 1s (never zero/negative — arm()'s own faulthandler call
    # rejects a non-positive delay) when the controller's own value is
    # already at or below the offset.
    assert worker_seconds_from(1.0) == 1.0
    assert worker_seconds_from(WORKER_OFFSET_SECONDS) == 1.0


def test_the_watchdog_stays_off_when_unset(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: REYN_STALL_TRACE_CI unset is a genuine no-op, even for a
    session whose own teardown hangs — #4986's own "opt-in, zero behavior
    change" requirement. Without this, ANY teardown hang anywhere (not just
    CI's own opted-in run) would start leaving dump files in every
    developer's own working tree.

    No duration: waits on the inner test's own marker file (a real event —
    proof the test body ran and only the deliberate teardown hang remains)
    rather than sleeping some guessed "long enough" span before checking
    absence."""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TEST_HANGS_AT_TEARDOWN)
    monkeypatch.delenv("REYN_STALL_TRACE_CI", raising=False)

    log_path = Path(pytester.path) / ".reyn-ci-stall-trace.log"
    marker_path = Path(pytester.path) / "test_started.marker"
    # `-s`: see test_a_session_teardown_hang_produces_a_stall_dump's own
    # comment — without it the fixture's `readline()` raises instead of
    # blocking, and this would stop being a genuine hang.
    proc = pytester.popen(
        [sys.executable, "-m", "pytest", "-q", "-s", "test_inner.py"],
        stdin=subprocess.PIPE,
    )
    try:
        while not marker_path.exists():
            time.sleep(0)
        # Now deterministically inside the deliberate teardown hang (the
        # test body already ran and returned) — no dump was ever armed
        # (env unset), so the file cannot exist and cannot come into
        # existence for as long as the hang holds.
        assert not log_path.exists(), (
            "#4986 REGRESSION: REYN_STALL_TRACE_CI unset must stay a true no-op"
        )
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait()
