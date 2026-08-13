"""Tests for scripts/_reyn_web_proc.py — the #268 reyn-web orphan-leak fix.

Tier 2: OS invariant — a managed reyn-web subprocess is torn down (the whole
process group, including children) on context exit and via atexit, so a driver
death does not leak orphans. Uses real subprocesses (no mock): a stand-in
``sleep`` process for the managed server, and a child it spawns to prove the
WHOLE group is killed (not just the direct child).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from tests._support.paths import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "_reyn_web_proc.py"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_context_manager_kills_process_on_exit(tmp_path):
    """Tier 2: managed_reyn_web kills the spawned process when the block exits."""
    sys.path.insert(0, str(SCRIPT.parent))
    import _reyn_web_proc as m

    with m.managed_reyn_web([sys.executable, "-c", "import time; time.sleep(120)"]) as proc:
        assert proc.poll() is None, "process should be alive inside the context"
        pid = proc.pid
    # #3748: unbounded (owner policy) -- wait for teardown to kill the
    # process. No terminating assert: the loop condition IS that check, so
    # a hang here surfaces via the kill stack showing this exact loop.
    while _alive(pid):
        time.sleep(0.1)


def test_group_kill_reaps_child_processes(tmp_path):
    """Tier 2: teardown group-kills children the server forked (own-session group).

    The stand-in 'server' spawns a long-lived grandchild and writes its PID to a
    file; after teardown both must be dead — proving start_new_session + killpg
    reaps the whole group, not just the direct child (the real-world case where
    reyn web forks workers).
    """
    sys.path.insert(0, str(SCRIPT.parent))
    import _reyn_web_proc as m

    pidfile = tmp_path / "child.pid"
    code = (
        "import os, sys, time, subprocess;"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
        f"open(r'{pidfile}', 'w').write(str(c.pid));"
        "time.sleep(120)"
    )
    with m.managed_reyn_web([sys.executable, "-c", code]) as proc:
        parent_pid = proc.pid
        # #3748: unbounded (owner policy) -- wait for the grandchild pid to
        # be recorded, needed before the context exits so teardown can
        # reap it too.
        while not (pidfile.exists() and pidfile.read_text().strip()):
            time.sleep(0.1)
    child_pid = int(pidfile.read_text().strip())
    # #3748: unbounded (owner policy) -- two SEPARATE waits, not one
    # compound OR: a compound loop's kill stack can't tell "the server
    # didn't die" from "group kill didn't reap the forked child" -- and
    # the latter, not the former, is this test's actual reason to exist
    # (own-session group + killpg reaping the WHOLE group, not just the
    # direct child). Splitting costs nothing: if both are already dead,
    # neither loop spins.
    while _alive(parent_pid):
        time.sleep(0.1)
    while _alive(child_pid):
        time.sleep(0.1)


def test_selftest_entrypoint_passes():
    """Tier 2: the module's own --selftest smoke returns 0 (spawn->alive->killed)."""
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "killed_after_context=True" in result.stdout
