"""Tier 1: Contract — reyn.dev.testing.session_deadline, the #5994 stage ①
graceful session-wide deadline.

Same style as tests/dev/test_stall_dump_4986.py: a REAL, isolated inner
pytest session (pytester's own subprocess seam), never pytester's in-process
runpytest() — the inner session's own ``pytest_sessionstart`` must run in a
genuinely separate process for this to mean anything.

No sleep(N) the assertion depends on: the "deadline already reached" case
below uses ``REYN_TEST_SESSION_DEADLINE_S=0``, which the module sets
synchronously in ``pytest_sessionstart`` (no ``threading.Timer``, no thread
scheduling to race — see that module's own "WHY A DEADLINE <= 0 STOPS
SYNCHRONOUSLY" docstring section). An earlier version of this test used a
real ``threading.Timer`` plus a wall-clock margin to force the deadline to
land mid-item1; that version went RED in actual CI (a 17,000+-test
``-n auto`` run under real CPU contention delayed the background Timer
thread past the test's own margin) — exactly the failure mode CLAUDE.md's
testing policy's Floor rule warns about ("no sleep(N) the assertion depends
on ... to let a task settle"). The fix removes the wall-clock dependency
instead of widening the margin.

Disclosed gap (test review Q4): this file exercises the synchronous
(``deadline_s <= 0``) branch only. The ``threading.Timer`` branch used when
CI's own arithmetic computes a POSITIVE deadline (the actually-deployed
case) is not independently re-proven here — it delegates to
``threading.Timer`` (stdlib) and to ``session.shouldstop`` firing between
items, the same built-in mechanism ``-x``/``--maxfail`` already rely on
throughout this codebase's own test suite. Both branches call the identical
``_stop()`` closure; only *when* it runs differs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_INNER_CONFTEST = 'pytest_plugins = ["reyn.dev.testing.session_deadline"]\n'

_INNER_TEST_TWO_ITEMS = """
from pathlib import Path

def test_1():
    Path("item1_started.marker").write_text("x")

def test_2():
    Path("item2_started.marker").write_text("x")
"""


def _run_inner(pytester: pytest.Pytester) -> str:
    proc = pytester.popen(
        [sys.executable, "-m", "pytest", "-q", "-s", "test_inner.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    stdout, _ = proc.communicate()
    return stdout


def test_an_already_reached_deadline_stops_collection_before_any_item_and_still_reports(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: the #5994 ① mechanism's synchronous branch — an already-past
    deadline (``<= 0``) sets ``session.shouldstop`` before collection even
    starts. pytest's own ``pytest_collectstart`` hook (``_pytest/main.py``'s
    ``Session``, ``tryfirst=True``) checks ``shouldstop`` and raises
    ``Interrupted`` immediately, so NEITHER item runs — this is stronger
    than "stop before the next one" (that weaker property only applies once
    collection has already produced items and one is mid-execution, the
    ``threading.Timer`` branch this file does not independently re-test —
    see the module docstring at the top of this file). What this test DOES
    prove: pytest still prints a normal, legible summary ("no tests ran")
    naming the deadline — not zero output, not a crash — #5994's own
    finding for the external-`timeout`-kill shape this replaces.

    Strip-falsifier: revert session_deadline.py's `pytest_sessionstart` to
    a no-op (or delete the conftest wiring) and this goes red — both items
    run (item2_started.marker appears) and the summary line no longer names
    a deadline."""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TEST_TWO_ITEMS)
    monkeypatch.setenv("REYN_TEST_SESSION_DEADLINE_S", "0")

    stdout = _run_inner(pytester)

    item1_started = Path(pytester.path) / "item1_started.marker"
    item2_started = Path(pytester.path) / "item2_started.marker"
    assert not item1_started.exists(), (
        "an already-past deadline must stop collection before item1 too"
    )
    assert not item2_started.exists()
    assert "#5994: session-wide deadline" in stdout
    assert "no tests ran" in stdout, (
        f"a normal, legible summary must still print — got: {stdout!r}"
    )


def test_an_unset_deadline_changes_nothing_about_a_normal_run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: the acceptance test's other direction — REYN_TEST_SESSION_
    DEADLINE_S unset (the default for every non-CI, and every CI run that
    never reaches the deadline) means zero behavior change: both items run,
    no extra output, no named deadline anywhere in the summary."""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TEST_TWO_ITEMS)
    monkeypatch.delenv("REYN_TEST_SESSION_DEADLINE_S", raising=False)

    stdout = _run_inner(pytester)

    item2_started = Path(pytester.path) / "item2_started.marker"
    assert item2_started.exists(), "unset deadline must not stop collection"
    assert "#5994" not in stdout
    assert "2 passed" in stdout
