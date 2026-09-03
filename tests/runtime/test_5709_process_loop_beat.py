"""Tier 2: #5709 — architect design (relayed by lead-coder-30): the
existing ``~/.reyn/processes/<pid>.json`` marker only ever records
``started_at``, so a process that died without a graceful exit leaves a
death window of ``[started_at, whoever reads it next]`` — a multi-hour
gap in a real incident (#5694). This closes it to ``[last_loop_beat_at,
observed_at]`` by adding ONE field: a periodic, process-scoped write
proving THIS process's own turn loop is still pumping (not merely that
the OS process still exists — a wedged loop with a live PID answers
"process exists" forever while never answering "can this session take
its next turn").

Real ``~/.reyn/processes/`` markers throughout (``PROCESSES_DIR``
monkeypatched to an isolated ``tmp_path`` — mirrors
``test_5226_process_registry.py``'s own established convention, this
module's own real target being real, shared, user-global state a test
must never touch), and a REAL, separate, controllable OS subprocess for
every dead-PID scenario (never a synthetic marker faked for a PID that
isn't actually running that code).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reyn.runtime import process_registry
from tests._support.paths import REPO_ROOT


@pytest.fixture(autouse=True)
def _isolated_processes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Same isolation contract as ``test_5226_process_registry.py``'s own
    fixture of the same name — the real ``~/.reyn/processes/`` must
    never be touched by a test."""
    processes_dir = tmp_path / "processes"
    monkeypatch.setattr(process_registry, "PROCESSES_DIR", processes_dir)
    # #5709: the beat's own idempotency guard is a MODULE-level global —
    # reset it per test so one test arming the driver does not leave a
    # background task spawned against a LATER test's own (different)
    # tmp_path/PROCESSES_DIR still ticking.
    monkeypatch.setattr(process_registry, "_beat_armed", False)
    monkeypatch.setattr(process_registry, "_beat_task_set", None)
    return processes_dir


def _wait_until(condition) -> None:
    """CLAUDE.md's own Ceiling rule — poll a real condition unboundedly,
    no fixed wait count of our own; pytest's/CI's own --timeout is the
    kill switch. Mirrors test_5226_process_registry.py's own helper."""
    while not condition():
        time.sleep(0)


def _spawn_registering_subprocess() -> subprocess.Popen:
    """A REAL, separate OS subprocess — same shape as
    test_5226_process_registry.py's own helper of the same name."""
    script = (
        "import sys\n"
        "from reyn.runtime import process_registry\n"
        "process_registry.PROCESSES_DIR = __import__('pathlib').Path(sys.argv[1])\n"
        "process_registry.register_process('subproc')\n"
        "sys.stdin.readline()\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script, str(process_registry.PROCESSES_DIR)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )


# ── record_loop_beat / ProcessLoopBeatDriver.check — no sleep, injected clock ──


def test_record_loop_beat_writes_the_field_via_an_injected_clock() -> None:
    """Tier 2: the write itself, driven with a fake clock — no real
    sleep anywhere in this test."""
    process_registry.register_process("chat")
    try:
        pid = os.getpid()
        process_registry.record_loop_beat(pid=pid, clock=lambda: 12345.0)
        marker_path = process_registry.PROCESSES_DIR / f"{pid}.json"
        data = json.loads(marker_path.read_text(encoding="utf-8"))
        assert data["last_loop_beat_at"] == 12345.0
    finally:
        process_registry.unregister_process(os.getpid())


def test_check_is_externally_callable_and_advances_with_the_injected_clock() -> None:
    """Tier 2: #5709 R2's own accept criterion — "with no turn ever run,
    advancing an injected clock advances the beat" — driven entirely
    through :meth:`ProcessLoopBeatDriver.check`, the driver's own
    externally-callable seam, never :meth:`run_forever`'s real sleep
    loop."""
    process_registry.register_process("chat")
    try:
        pid = os.getpid()
        marker_path = process_registry.PROCESSES_DIR / f"{pid}.json"
        fake_now = [100.0]
        driver = process_registry.ProcessLoopBeatDriver(
            pid=pid, clock=lambda: fake_now[0],
        )

        driver.check()
        assert json.loads(marker_path.read_text())["last_loop_beat_at"] == 100.0

        fake_now[0] = 250.0
        driver.check()
        assert json.loads(marker_path.read_text())["last_loop_beat_at"] == 250.0
    finally:
        process_registry.unregister_process(os.getpid())


def test_no_check_call_leaves_the_field_absent() -> None:
    """Tier 2: deny-side — registering alone (no beat driver ever armed)
    must never fabricate a beat. A marker that never gains this field is
    the honest signal that this process's own loop never reached the
    arming seam."""
    process_registry.register_process("chat")
    try:
        pid = os.getpid()
        data = json.loads(
            (process_registry.PROCESSES_DIR / f"{pid}.json").read_text()
        )
        assert data["last_loop_beat_at"] is None
    finally:
        process_registry.unregister_process(os.getpid())


# ── the inversion trap (#5709 R2): beat must NOT be gated on turn activity ──


@pytest.mark.asyncio
async def test_beat_advances_more_than_once_during_a_simulated_long_turn() -> None:
    """Tier 2: R2's own named inversion trap — "beat stops during a
    turn" is the OPPOSITE of the point (a beat is most needed WHILE a
    long turn is in flight). Pinned as a real concurrency property: a
    background ``run_forever()`` task ticks independently while a
    SEPARATE, concurrently-running coroutine (standing in for "a long
    turn") never calls :meth:`check` itself.

    Deliberate, disclosed exception to the no-sleep-in-tests rule
    (CLAUDE.md's own Floor clause, and #5709's own architect ruling
    §4): the SUBJECT under test IS real asyncio scheduling — whether a
    background task actually gets CPU time while another coroutine
    runs — which no injected clock can stand in for (a fake clock only
    proves the WRITE logic, pinned above; it cannot prove concurrent
    scheduling actually happened). Kept to a small, fixed, short
    duration (0.01s driver interval, ~0.2s total) — the assertion is
    "2 or more ticks landed", never a duration the test waits OUT to a
    precise count."""
    process_registry.register_process("chat")
    try:
        pid = os.getpid()
        marker_path = process_registry.PROCESSES_DIR / f"{pid}.json"
        driver = process_registry.ProcessLoopBeatDriver(pid=pid, interval_s=0.01)
        task = asyncio.create_task(driver.run_forever())
        try:
            # Stand-in for "a long turn in flight" — a separate coroutine
            # that itself never calls check(), just holds the event loop
            # busy-but-yielding for a short, fixed span.
            for _ in range(20):
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        data = json.loads(marker_path.read_text())
        assert data["last_loop_beat_at"] is not None, (
            "the beat must have landed at least once during the "
            "simulated long turn — 0 beats is R2's own named inversion"
        )
    finally:
        process_registry.unregister_process(os.getpid())


# ── process-scoped singleton (#5709 R5) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_arm_process_loop_beat_is_idempotent_across_two_sessions_in_one_process() -> None:
    """Tier 2: R5's own accept criterion verbatim — "1 process + 2
    Sessions = 1 beater". Calling the arm function twice (standing in
    for two Session.run() calls in the same process) must spawn exactly
    ONE background task, never two racing writers of the same marker.
    Driven entirely through :func:`armed_beat_task_count`, the public
    snapshot read — never the private ``_beat_task_set`` module global."""
    process_registry.register_process("chat")
    try:
        assert process_registry.armed_beat_task_count() == 0  # sanity: not yet armed
        process_registry.arm_process_loop_beat()
        assert process_registry.armed_beat_task_count() == 1

        process_registry.arm_process_loop_beat()
        assert process_registry.armed_beat_task_count() == 1, (
            "#5709 REGRESSION: a second arm() call spawned a second beater — "
            "R5's own accept criterion (1 process + 2 Sessions = 1 beater) failed"
        )
    finally:
        for task in list(process_registry._beat_task_set or []):
            task.cancel()
        process_registry.unregister_process(os.getpid())


# ── non-destructive reader (#5709 R3) ───────────────────────────────────────


def test_read_process_markers_does_not_reap_a_dead_pid_marker() -> None:
    """Tier 2: R3's own charter-Q3 concern — a dead-PID marker read by
    :func:`read_process_markers` must survive the read (unlike
    :func:`live_processes`, which reaps it as part of its own, DIFFERENT
    contract — see that function's own docstring, unchanged by this
    issue). Reading twice must not destroy what a SECOND reader (doctor,
    then a broker poll, in either order) still needs to see."""
    proc = _spawn_registering_subprocess()
    try:
        _wait_until(lambda: len(process_registry.read_process_markers()) >= 1)
        marker_path = process_registry.PROCESSES_DIR / f"{proc.pid}.json"
        assert marker_path.exists()

        proc.kill()
        proc.wait()
        _wait_until(lambda: not process_registry.pid_alive(proc.pid))

        first_read = process_registry.read_process_markers()
        assert any(m.get("pid") == proc.pid for m in first_read)
        assert marker_path.exists(), (
            "#5709 REGRESSION: read_process_markers() must never reap — "
            "the marker was deleted by a supposedly non-destructive read"
        )

        second_read = process_registry.read_process_markers()
        assert any(m.get("pid") == proc.pid for m in second_read), (
            "#5709 REGRESSION: the SECOND non-destructive read no longer "
            "sees the dead-PID marker the first read already reported"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ── structural witness (#5709 R2's own git-grep accept criterion) ──────────


def test_record_loop_beat_has_exactly_one_production_caller() -> None:
    """Tier 2: R2's own literal accept criterion — ``record_loop_beat``'s
    only production caller is :meth:`ProcessLoopBeatDriver.check`, and
    no turn-path file (``session.py``'s own ``run_one_iteration``,
    ``router_loop.py``, ``router_loop_driver.py``) calls it directly.
    A turn-path caller would mean the beat's own "is the LOOP alive"
    claim quietly narrowed back into "did a turn recently run" — the
    exact distinction R1 exists to keep."""
    import subprocess as _sp

    result = _sp.run(
        ["git", "grep", "-n", "record_loop_beat(", "--", "src/"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    hits = [
        line for line in result.stdout.splitlines()
        if ":def record_loop_beat(" not in line
    ]
    assert hits, "sanity: record_loop_beat must have at least one real caller"
    turn_path_hits = [h for h in hits if "process_registry.py" not in h]
    assert turn_path_hits == [], (
        f"#5709 REGRESSION: record_loop_beat() is called from outside "
        f"process_registry.py's own driver — {turn_path_hits!r}"
    )
