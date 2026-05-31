"""Tests for scripts/_reyn_web_proc.py — the #268 reyn-web orphan-leak fix.

Tier 2: OS invariant — a managed reyn-web subprocess is torn down (the whole
process group, including children) on context exit and via atexit, so a driver
death does not leak orphans. Uses real subprocesses (no mock): a stand-in
``sleep`` process for the managed server, and a child it spawns to prove the
WHOLE group is killed (not just the direct child).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "_reyn_web_proc.py"


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
    # give teardown a moment
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid), "process must be dead after context exit"


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
        # wait for the grandchild pid to be recorded
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
    child_pid = int(pidfile.read_text().strip())
    for _ in range(50):
        if not _alive(parent_pid) and not _alive(child_pid):
            break
        time.sleep(0.1)
    assert not _alive(parent_pid), "managed server must be dead"
    assert not _alive(child_pid), "forked child must also be reaped (group kill)"


def test_selftest_entrypoint_passes():
    """Tier 2: the module's own --selftest smoke returns 0 (spawn->alive->killed)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "killed_after_context=True" in result.stdout
