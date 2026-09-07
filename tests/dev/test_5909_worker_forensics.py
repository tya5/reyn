"""Tier 2: #5909 — ``reyn.dev.testing.worker_forensics`` names how an xdist
worker died and what it was running, from the files it leaves behind.

Real files under a tmp cwd, a real finished ``subprocess.Popen`` for the
exit-code reader, this test's own real ``request.node`` for the setup hook.
ONE disclosed stand-in: the xdist ``node.gateway._io`` attribute chain the
exit-code reader walks is a hand-rolled attribute holder around that REAL
``Popen`` — a live ``WorkerController`` needs a spawned execnet gateway,
which is not cheaply constructible, and the reader's own contract is
exactly "follow this shape, never raise". The one thing no real
collaborator exposes as state (that the isolation fixture cancels a
pending stall dump only where #4986's controller watchdog cannot be) is
gated on the fixture's own source, the same ``inspect.getsource`` shape
``test_5364_media_store_flush_barrier.py`` uses for a timer-driven seam.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from reyn.dev.testing import worker_forensics as wf


def test_node_down_line_names_the_signal_the_last_test_and_what_ran_before() -> None:
    """Tier 2: the one line a reader needs — a negative exit code is the
    killing signal (``-9`` named SIGKILL: the host, never reyn); the last
    recorded test is separated from the ones before it."""
    line = wf.format_node_down(
        "gw2", "Not properly terminated", -9, ["suite/a.py::t1", "suite/b.py::t2", "suite/c.py::t3"],
    )
    assert "worker=gw2" in line
    assert "returncode=-9 (signal 9 SIGKILL)" in line
    assert "last_test=suite/c.py::t3" in line
    assert "before_it=['suite/a.py::t1', 'suite/b.py::t2']" in line

    quiet = wf.format_node_down("gw0", "Not properly terminated", None, [])
    assert "returncode=None" in quiet and "last_test=<no test recorded>" in quiet
    assert "signal" not in quiet


def test_trace_records_each_test_start_and_the_peak_rss_and_recent_skips_the_peak_line(
    tmp_path: Path,
) -> None:
    """Tier 2: one nodeid per test start per worker file; the session-end
    ``peak_rss_mb=`` line is part of the trace but never counted as a
    test by ``recent_tests``."""
    for nodeid in ("suite/x.py::t1", "suite/x.py::t2", "suite/y.py::t3"):
        wf.record_test_start("gw1", nodeid, root=tmp_path)
    wf.record_peak_rss("gw1", 1234.6, root=tmp_path)

    trace = (tmp_path / wf.TRACE_DIR / "gw1.log").read_text(encoding="utf-8").splitlines()
    assert trace == ["suite/x.py::t1", "suite/x.py::t2", "suite/y.py::t3", "peak_rss_mb=1235"]
    assert wf.recent_tests("gw1", root=tmp_path, n=2) == ["suite/x.py::t2", "suite/y.py::t3"]
    assert wf.recent_tests("gw9", root=tmp_path) == [], "an unknown worker has no trace, not an error"


def test_returncode_of_reads_a_finished_process_through_execnets_popen_shape() -> None:
    """Tier 2: the exit-code reader follows execnet's ``gateway._io.popen``
    shape to a REAL ``subprocess.Popen`` that has already ended — a
    signal-killed process reports the negative signal number, which is
    exactly the datum #5909's crashed workers never surfaced. Any missing
    link in the shape yields ``None``, never an exception (the hook runs in
    execnet's receiver thread)."""
    proc = subprocess.Popen([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"])
    proc.wait()

    class _IO:
        popen = proc

    class _Gateway:
        _io = _IO()
        id = "gw7"

    class _Node:
        gateway = _Gateway()

    assert wf.returncode_of(_Node()) == -9
    assert wf.returncode_of(object()) is None


def test_the_setup_hook_records_this_very_test_under_its_worker_id(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: driven with this test's OWN real ``request.node`` — the hook
    reads the worker id off ``config.workerinput`` (xdist's discriminator)
    and appends the nodeid under the cwd. Outside xdist (no
    ``workerinput``) it writes nothing at all."""
    monkeypatch.chdir(tmp_path)
    item = request.node

    had_workerinput = hasattr(item.config, "workerinput")
    if not had_workerinput:
        monkeypatch.setattr(item.config, "workerinput", {"workerid": "gwX"}, raising=False)
    wid = wf.worker_id(item.config)
    assert wid, "test setup sanity: a worker id must resolve"

    wf.pytest_runtest_setup(item)

    assert wf.recent_tests(wid, root=tmp_path) == [item.nodeid]

    if not had_workerinput:
        monkeypatch.delattr(item.config, "workerinput", raising=False)
        assert wf.worker_id(item.config) is None
        wf.pytest_runtest_setup(item)  # serial run: nothing written
        assert wf.recent_tests(wid, root=tmp_path) == [item.nodeid]


def test_the_isolation_fixture_cancels_a_pending_stall_dump_only_where_the_ci_watchdog_cannot_be() -> None:
    """Tier 2: hygiene gate for the #5909 leak class (a test that runs the
    real ``stall_trace.arm`` with ``disarm`` stubbed leaves a one-shot
    pending against an fd its own ``finally`` then closes). A negative
    timing witness ("nothing arrives") would be a wait the assertion
    depends on, so this reads the fixture's own source instead: it must
    ``disarm()`` after the yield, and only inside an xdist worker or with
    ``REYN_STALL_TRACE_CI`` unset — the one process-wide timer on a serial
    CI run is #4986's session watchdog, which must survive every test."""
    import tests.conftest as conftest

    src = inspect.getsource(conftest._isolate_stall_trace_file_handler_registration)
    after_yield = src.split("yield", 1)[1]
    assert 'if hasattr(request.config, "workerinput") or not os.environ.get("REYN_STALL_TRACE_CI"):' in after_yield
    assert "stall_trace.disarm()" in after_yield
