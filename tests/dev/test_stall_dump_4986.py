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
        while not (log_path.exists() and log_path.stat().st_size > 0):
            time.sleep(0)
        content = log_path.read_text()
    finally:
        assert proc.stdin is not None
        proc.stdin.close()  # releases _hang_forever's readline()
        proc.wait()

    assert "Thread" in content, (
        f"the dump file should contain a real faulthandler thread-stack "
        f"dump even for an atexit-time hang, got {content!r}"
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
        while not (log_path.exists() and log_path.stat().st_size > 0):
            time.sleep(0)
        content = log_path.read_text()
    finally:
        assert proc.stdin is not None
        proc.stdin.close()  # releases _hang_forever_at_teardown's readline()
        proc.wait()

    assert "Thread" in content, (
        f"the dump file should contain a real faulthandler thread-stack "
        f"dump, got {content!r}"
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
