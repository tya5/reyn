"""Tier 2: #5226 — reyn cannot currently answer "how many of me are alive,
and whose" without an operator manually shelling out to ``ps``+``lsof``.

Owner's own observation (2026-08-21, relayed by lead-coder): "I only
launched one reyn session, so the rest are your own cleanup misses."
lead-coder's own real-machine trace confirmed it — 12 ``reyn``/``reyn:chat``
processes, 11 abandoned, the oldest 11 days, and had to reconstruct "whose"
by hand via ``ps -eo pid,etime,comm`` + ``lsof -a -p <pid> -d cwd`` (reyn
rewrites its own process NAME, ``reyn.runtime.proctitle``, so ``comm``
alone cannot say which workspace a listed PID belongs to). Design ruled by
lead-coder (issue #5226's own final comment, before implementation):
``~/.reyn/processes/<pid>.json`` markers, PID-keyed (never a shared subtree
— unlike ``~/.reyn/plugins/<name>/``'s own known #3212 clobber hazard),
``pid_alive`` reuse (not reinvented — ``data/index/build_lock.py``'s
version, not ``api/safe/process.py``'s sandbox-scoped one), "who" =
ppid+cwd+subcommand only (the same data lead-coder manually reconstructed —
no fabricated attribution, no full argv/paths).

Matches lead-coder's own 4 acceptance criteria exactly:
① 2 processes registered → 2 visible; one exits → exactly 1 visible.
② a dead-PID marker (left behind by a non-graceful exit) is reaped on the
  next read, not kept forever.
③ full path/argv never appears anywhere in a marker's own content — a
  CONTENT inspection, not a behavioral strip-falsify.
④ strip-falsify: removing the marker-write call makes ① go red.

Real processes throughout — a genuinely separate, real, controllable OS
subprocess for the "two processes" case (spawned via ``subprocess.Popen``,
released via its own stdin so no sleep/duration is ever waited on — CLAUDE.md's
own Ceiling/Floor rule), never a synthetic marker faked in-process for a PID
that isn't actually running that code. ``PROCESSES_DIR`` monkeypatched to a
``tmp_path`` for every test — this module's own real target,
``~/.reyn/processes/``, is real, shared, user-global state that tests must
never touch."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reyn.runtime import process_registry


@pytest.fixture(autouse=True)
def _isolated_processes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test in this file gets its own ``PROCESSES_DIR`` — the real
    ``~/.reyn/processes/`` is shared, user-global state (mirrors
    ``~/.reyn/plugins/<name>/``'s own #3212 concurrency hazard this
    module's own docstring names) and must never be touched by a test."""
    processes_dir = tmp_path / "processes"
    monkeypatch.setattr(process_registry, "PROCESSES_DIR", processes_dir)
    return processes_dir


def _spawn_registering_subprocess() -> subprocess.Popen:
    """A REAL, separate OS subprocess that imports this exact module,
    calls ``register_process`` for ITS OWN real PID (genuinely different
    from this test process's own PID), then blocks on a real stdin read
    (never a sleep) until the test releases it by closing stdin. The
    PROCESSES_DIR override is threaded through an env var, since a
    subprocess has no access to this test's own monkeypatched module
    attribute."""
    script = (
        "import sys\n"
        "from reyn.runtime import process_registry\n"
        "process_registry.PROCESSES_DIR = __import__('pathlib').Path(sys.argv[1])\n"
        "process_registry.register_process('subproc')\n"
        "sys.stdin.readline()\n"  # blocks for real — released by closing stdin
    )
    return subprocess.Popen(
        [sys.executable, "-c", script, str(process_registry.PROCESSES_DIR)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )


def _wait_until(condition) -> None:
    """CLAUDE.md's own Ceiling rule: poll a real condition unboundedly, no
    sleep-based fixed wait count or custom timeout of our own — pytest's
    own ``--timeout`` (or CI's) is the kill switch if this genuinely
    never resolves."""
    while not condition():
        time.sleep(0)  # yield the GIL, not a duration this assertion depends on


def test_two_real_processes_both_visible_then_one_exits_and_drops_to_one() -> None:
    """Tier 2: acceptance ① — 2 real processes registered → exactly 2
    markers visible; one exits gracefully (its own atexit cleanup fires)
    → exactly 1 remains."""
    process_registry.register_process("self")
    proc = _spawn_registering_subprocess()
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 2)
        pids = {e["pid"] for e in process_registry.live_processes()}
        assert pids == {os.getpid(), proc.pid}, (
            f"#5226 REGRESSION: expected exactly this process's own pid and "
            f"the subprocess's, got {pids!r}"
        )

        assert proc.stdin is not None  # sanity: PIPE was requested above
        proc.stdin.close()  # release the real blocked read — a graceful exit
        proc.wait(timeout=10)

        _wait_until(lambda: len(process_registry.live_processes()) == 1)
        remaining = process_registry.live_processes()
        assert remaining[0]["pid"] == os.getpid(), (
            "#5226 REGRESSION: after the subprocess exited, the SELF marker "
            f"should be the only one left — got {remaining!r}"
        )
    finally:
        process_registry._cleanup(os.getpid())
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_a_dead_pid_marker_is_reaped_on_the_next_read(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: acceptance ② — a marker left behind by a process that did
    NOT exit gracefully (no atexit ever ran — the real shape a SIGKILL or
    hard crash leaves) is reaped the next time anything reads the
    registry, once its PID is confirmed dead."""
    proc = _spawn_registering_subprocess()
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 1)
        marker_path = _isolated_processes_dir / f"{proc.pid}.json"
        assert marker_path.exists()  # sanity: the real marker is really there

        proc.kill()  # SIGKILL — atexit never runs; the marker is left behind
        proc.wait(timeout=10)

        _wait_until(lambda: not process_registry.pid_alive(proc.pid))

        remaining = process_registry.live_processes()
        assert remaining == [], (
            f"#5226 REGRESSION: a confirmed-dead PID's marker was not reaped "
            f"on read — got {remaining!r}"
        )
        assert not marker_path.exists(), (
            "the stale marker file itself should have been deleted by the read, "
            "not just excluded from the returned list"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_marker_content_never_carries_argv_or_a_path_beyond_cwd(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: acceptance ③ — a CONTENT inspection (not a behavioral
    strip-falsify): the marker this process writes about itself carries
    EXACTLY {pid, ppid, cwd, subcommand, started_at} and nothing else —
    no full argv, no path beyond cwd. Mirrors ``proctitle.py``'s own
    explicit stance against leaking more than the minimum."""
    process_registry.register_process("chat")
    try:
        marker_path = _isolated_processes_dir / f"{os.getpid()}.json"
        raw = json.loads(marker_path.read_text(encoding="utf-8"))

        assert set(raw.keys()) == {"pid", "ppid", "cwd", "subcommand", "started_at"}, (
            f"#5226 REGRESSION: the marker carries an unexpected field — "
            f"got keys {sorted(raw.keys())!r}"
        )
        # `subcommand` must be the bare word passed in — never the full
        # argv this process was actually launched with (pytest's own,
        # which carries this test file's real absolute path).
        assert raw["subcommand"] == "chat"
        this_process_own_argv_path = str(Path(sys.argv[0]).resolve())
        assert this_process_own_argv_path not in json.dumps(raw), (
            "#5226 REGRESSION: the marker's own JSON content contains this "
            "process's real argv[0] path — argv must never be recorded"
        )
    finally:
        process_registry._cleanup(os.getpid())
