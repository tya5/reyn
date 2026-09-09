"""Tier 1: Contract — reyn.dev.testing.session_deadline, the #5994 stage ①
graceful session-wide deadline.

Same style as tests/dev/test_stall_dump_4986.py: a REAL, isolated inner
pytest session (pytester's own subprocess seam), never pytester's in-process
runpytest() — the inner session's own ``pytest_sessionstart``/threading.Timer
must run in a genuinely separate process for this to mean anything.

No sleep(N) the assertion depends on: the outer test blocks on a real FIFO
(released deterministically by this test, not a timer) until the inner
session's FIRST item has genuinely started, and separately busy-polls real
elapsed wall time against ``REYN_TEST_SESSION_DEADLINE_S`` itself — the
mechanism's own injected clock, not a guessed settle time — before releasing
that item. This guarantees the deadline has already fired inside the
background Timer thread before the still-running item is allowed to finish,
so whether the SECOND item ever starts is not a timing race.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_INNER_CONFTEST = 'pytest_plugins = ["reyn.dev.testing.session_deadline"]\n'

_INNER_TEST_TWO_ITEMS = """
from pathlib import Path

def test_1():
    Path("item1_started.marker").write_text("x")
    with open("release_1.fifo") as f:
        f.read()
    Path("item1_done.marker").write_text("x")

def test_2():
    Path("item2_started.marker").write_text("x")
"""


def test_a_deadline_reached_mid_session_stops_collection_and_still_reports(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: the #5994 ① mechanism itself — hitting the deadline lets the
    CURRENTLY-running item finish and report, then stops collecting more,
    then prints pytest's own normal summary (not zero output, #5994's own
    finding for the external-`timeout`-kill shape this replaces).

    Strip-falsifier: revert session_deadline.py's `pytest_sessionstart` to a
    no-op (or delete the conftest wiring) and this goes red — test_2 runs
    (item2_started.marker appears) and the summary line no longer names a
    deadline."""
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TEST_TWO_ITEMS)
    fifo_path = Path(pytester.path) / "release_1.fifo"
    os.mkfifo(fifo_path)

    deadline_s = 0.2
    monkeypatch.setenv("REYN_TEST_SESSION_DEADLINE_S", str(deadline_s))

    start = time.monotonic()
    proc = pytester.popen(
        [sys.executable, "-m", "pytest", "-q", "-s", "test_inner.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    try:
        item1_started = Path(pytester.path) / "item1_started.marker"
        while not item1_started.exists():
            time.sleep(0)  # unbounded poll on a real condition, no ceiling

        # Real elapsed time against the mechanism's OWN injected constant
        # (not a guessed settle duration) — ensures the background Timer
        # inside the still-blocked item1 has already fired before we let
        # item1 finish, so item2 never starting is not a race.
        while time.monotonic() - start < deadline_s * 3:
            time.sleep(0)

        with open(fifo_path, "w") as f:
            f.write("go")
    finally:
        stdout, _ = proc.communicate()

    item1_done = Path(pytester.path) / "item1_done.marker"
    item2_started = Path(pytester.path) / "item2_started.marker"
    assert item1_done.exists(), "item1 must finish and report, not be killed"
    assert not item2_started.exists(), (
        "item2 must never start once the deadline fired mid-item1"
    )
    assert "#5994: session-wide deadline" in stdout
    assert "1 passed" in stdout, (
        f"a normal pytest summary must still print — got: {stdout!r}"
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
    fifo_path = Path(pytester.path) / "release_1.fifo"
    os.mkfifo(fifo_path)
    monkeypatch.delenv("REYN_TEST_SESSION_DEADLINE_S", raising=False)

    proc = pytester.popen(
        [sys.executable, "-m", "pytest", "-q", "-s", "test_inner.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    try:
        item1_started = Path(pytester.path) / "item1_started.marker"
        while not item1_started.exists():
            time.sleep(0)
        with open(fifo_path, "w") as f:
            f.write("go")
    finally:
        stdout, _ = proc.communicate()

    item2_started = Path(pytester.path) / "item2_started.marker"
    assert item2_started.exists(), "unset deadline must not stop collection"
    assert "#5994" not in stdout
    assert "2 passed" in stdout
