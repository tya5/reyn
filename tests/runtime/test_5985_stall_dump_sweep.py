"""Tier 2: dead-pid `stall_dump.<pid>.log` sweep (#5985's remaining scope).

#5981/#5983/#6005 closed the `.sb` sandbox-profile leak the same way: a
pid-scoped destination plus a liveness-gated sweep of dead siblings,
never age-based, no config knob. lead-coder found #5997 reproduced the
identical unbounded-leftover class for `stall_dump.<pid>.log` (a pid was
put in this filename too, but nothing ever sweeps it) -- this file mirrors
`tests/security/test_sandbox_seatbelt.py`'s own #5985 tests, same shape,
different mechanism (`unlink` on a flat file here, vs. `rmtree` on a
pid-subdirectory there).
"""
from __future__ import annotations

import logging

import pytest

from reyn.runtime import loop_tripwire
from reyn.runtime.loop_tripwire import StallDumpArm, _sweep_dead_pid_stall_dumps


@pytest.fixture(autouse=True)
def _reset_dead_pid_sweep_flag():
    """`_sweep_dead_pid_stall_dumps` runs at most ONCE per process — without
    resetting it, whichever test in this file runs first consumes that
    "once" for every test after it, silently no-opping the sweep in every
    test that means to exercise it (same rationale as
    `test_sandbox_seatbelt.py`'s identically-named fixture)."""
    loop_tripwire._swept_dead_pid_stall_dumps = False
    yield
    loop_tripwire._swept_dead_pid_stall_dumps = False


def _dead_pid() -> int:
    """A real, guaranteed-not-alive pid — spawn a trivial subprocess and
    let it exit, then use its own pid. Cheaper and more honest than
    guessing a large integer that MIGHT collide with something real on a
    busy machine."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_sweep_removes_a_dead_pids_stall_dump_file(tmp_path):
    """Tier 2: a sibling `stall_dump.<pid>.log` whose owning process has
    already exited IS removed — the same "who stops this if it repeats"
    answer #5985 already gave the `.sb` cache."""
    dead = tmp_path / f"stall_dump.{_dead_pid()}.log"
    dead.write_text("stale stack dump", encoding="utf-8")
    own_path = tmp_path / "stall_dump.999999999.log"  # never actually opened

    _sweep_dead_pid_stall_dumps(str(own_path), logger=logging.getLogger(__name__))

    assert not dead.exists()


def test_sweep_does_not_remove_a_live_pids_stall_dump_file(tmp_path):
    """Tier 2: strip-falsify's counterpart — a sibling `stall_dump.<pid>.log`
    whose owning process is ALIVE (this test's own real parent process, a
    genuinely distinct running pid) survives the sweep untouched.

    NON-VACUITY (strip-falsified locally, in-file Edit -> run -> Edit
    back): replacing the `pid_alive(pid)` check with an unconditional
    False makes this assertion fail."""
    import os

    live_pid = os.getppid()
    live = tmp_path / f"stall_dump.{live_pid}.log"
    live.write_text("still needed", encoding="utf-8")
    own_path = tmp_path / "stall_dump.999999999.log"

    _sweep_dead_pid_stall_dumps(str(own_path), logger=logging.getLogger(__name__))

    assert live.exists(), (
        "a LIVE sibling's stall-dump file was removed — this is exactly the "
        "failure #5981/#5985's own investigation ruled out a blanket sweep over"
    )


def test_sweep_ignores_non_matching_names(tmp_path):
    """Tier 2: a stray file that doesn't match `stall_dump.<digits>.log`
    (reyn.log itself, a different process's unrelated file, a typo) is
    left alone rather than raising or being swept as if it were one of
    this mechanism's own files."""
    stray = tmp_path / "reyn.log"
    stray.write_text("operational log", encoding="utf-8")
    almost = tmp_path / "stall_dump.notapid.log"
    almost.write_text("x", encoding="utf-8")
    own_path = tmp_path / "stall_dump.999999999.log"

    _sweep_dead_pid_stall_dumps(str(own_path), logger=logging.getLogger(__name__))  # must not raise

    assert stray.exists()
    assert almost.exists()


def test_sweep_runs_at_most_once_per_process(tmp_path):
    """Tier 2: the module-level guard — a second call in the same process
    is a no-op even if a fresh dead-pid file appears in between."""
    own_path = tmp_path / "stall_dump.999999999.log"
    _sweep_dead_pid_stall_dumps(str(own_path), logger=logging.getLogger(__name__))  # consumes the "once"

    dead = tmp_path / f"stall_dump.{_dead_pid()}.log"
    dead.write_text("stale", encoding="utf-8")

    _sweep_dead_pid_stall_dumps(str(own_path), logger=logging.getLogger(__name__))  # must be a no-op

    assert dead.exists(), "the sweep ran a second time in the same process"


def test_sweep_logs_the_removed_count(tmp_path, caplog):
    """Tier 2: #5985 co-vet precedent (PR #6005) — the sweep's outcome must
    be observable, not silent on every branch.

    NON-VACUITY (strip-falsified locally, in-file Edit -> run -> Edit
    back): removing the `logger.info(...)` call makes this assertion fail
    — there would be nothing in the log to assert on."""
    dead = tmp_path / f"stall_dump.{_dead_pid()}.log"
    dead.write_text("stale", encoding="utf-8")
    own_path = tmp_path / "stall_dump.999999999.log"

    with caplog.at_level(logging.INFO, logger=__name__):
        _sweep_dead_pid_stall_dumps(str(own_path), logger=logging.getLogger(__name__))

    messages = [record.getMessage() for record in caplog.records]
    assert any("removed 1 dead-pid file" in m for m in messages), (
        f"the sweep's own outcome (1 file removed) left no observable trace — "
        f"records were: {messages}"
    )


def test_stall_dump_arm_open_triggers_the_sweep(tmp_path):
    """Tier 2: integration — a real `StallDumpArm.open()` call (the actual
    production chokepoint, both `app.py` and `server.py`) triggers the
    sweep as a side effect, not just the unit-level direct call above."""
    dead = tmp_path / f"stall_dump.{_dead_pid()}.log"
    dead.write_text("stale", encoding="utf-8")

    own_path = tmp_path / "stall_dump.888888888.log"
    arm = StallDumpArm.open(
        seconds=1.0, path=str(own_path), logger=logging.getLogger(__name__), label="test",
    )
    assert arm is not None

    assert not dead.exists()

    arm.close()
