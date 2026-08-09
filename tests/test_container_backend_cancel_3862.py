"""Tier 2: DockerEnvironmentBackend.run() actually stops the in-container
process on cancel (#3862), not just the host-side ``docker exec`` client.

Real subprocesses throughout, no live Docker daemon: a tiny local shim script
stands in for the ``docker`` binary — it discards the leading ``exec
<container>`` and runs the REST of the real argv directly on the local
machine (a REAL subprocess test double per the testing policy, not a fake
collaborator: ``run()``'s own cancel-aware code and
``_docker_kill_in_container``'s own real implementation both execute
unmodified, just pointed at this shim instead of the real ``docker`` binary).
This is the same "local runner" shape ``test_container_backend_1115_stage2.py``
already uses for the FS-op tests, applied to the exec + cancel path.
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from reyn.environment.container_backend import (
    DockerEnvironmentBackend,
    _docker_kill_in_container,
)
from reyn.security.sandbox.policy import SandboxPolicy


@pytest.fixture
def fake_docker_bin(tmp_path: Path) -> str:
    """A real, executable shim standing in for the `docker` binary:
    `fake-docker exec [-i] [-w DIR] <container> <real command...>` skips
    "exec", any flags (consuming -w's value too), and the container name,
    then runs the real command as a SEPARATE, DETACHED child process
    (``start_new_session=True``) and waits on it.

    Deliberately NOT ``os.execvp`` (process replacement): a real ``docker
    exec`` client and the workload it starts inside the container are two
    genuinely independent processes connected only by an I/O stream — killing
    the client does not kill the workload. An ``exec``-based shim would
    collapse them into the SAME OS process (exec replaces the image but
    keeps the PID), so killing the shim would trivially kill the workload
    too — a test built on that shim could not tell a correct fix from
    #3862's own bug shape (client-only kill), because both would look
    identical under an exec-collapsed shim. This was caught by falsify-
    verifying against the exact bug shape and finding the FIRST version of
    this shim couldn't distinguish them."""
    script = tmp_path / "fake-docker"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, subprocess\n"
        "args = sys.argv[1:]\n"
        "assert args[0] == 'exec'\n"
        "i = 1\n"
        "while i < len(args) and args[i].startswith('-'):\n"
        "    i += 2 if args[i] == '-w' else 1\n"
        "i += 1  # skip the container name\n"
        "child = subprocess.Popen(args[i:], start_new_session=True)\n"
        "sys.exit(child.wait())\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def _backend(fake_docker_bin: str, repo_dir: Path) -> DockerEnvironmentBackend:
    return DockerEnvironmentBackend(
        container="unused", repo_dir=str(repo_dir), docker_bin=fake_docker_bin,
    )


@pytest.mark.asyncio
async def test_cancel_actually_stops_the_real_process_not_just_the_client(
    fake_docker_bin: str, tmp_path: Path,
) -> None:
    """Tier 2: #3862 — the MAIN witness. A real, long-running child process
    is genuinely gone after cancel — not "the docker exec client returned",
    which would be true even if #3862's bug were still present (the client
    disconnecting does not, by itself, prove the workload stopped).

    The child writes its own PID to a marker file on start and touches a
    SECOND file every 0.1s in a loop — if cancel only killed the host-side
    client (the #3862 bug), this loop would keep running and keep touching
    the file. We assert the file stops growing AFTER cancel, using the
    child's OWN OS-level PID (independent of the pidfile mechanism under
    test) as the ground truth for "is it actually still running".
    """
    marker = tmp_path / "alive.count"
    marker.write_text("0")
    script = (
        "import os,time,sys\n"
        "p = sys.argv[1]\n"
        "n = 0\n"
        "while True:\n"
        "    n += 1\n"
        # #3963: atomic write (tmp -> os.replace), not a bare truncating
        # `open(p, 'w').write(...)` — the latter lets a concurrent reader
        # observe the file mid-truncate (content == "") and blow up on
        # `int("")`, a failure unrelated to this test's own cancel/liveness
        # claim. os.replace() is POSIX-atomic: any reader sees either the
        # complete old write or the complete new one, never a partial state.
        "    tmp = p + '.tmp'\n"
        "    open(tmp, 'w').write(str(n))\n"
        "    os.replace(tmp, p)\n"
        "    time.sleep(0.05)\n"
    )
    backend = _backend(fake_docker_bin, tmp_path)
    policy = SandboxPolicy(timeout_seconds=30)
    cancel_event = asyncio.Event()

    async def _cancel_once_actually_running() -> None:
        # Poll for the child to have genuinely started (marker > "0") rather
        # than a fixed sleep — `bash -l` login-shell startup (sourcing
        # profile files) plus two nested interpreter spawns take real,
        # variable wall-clock time under test-harness overhead; a fixed
        # delay tuned to pass locally would be exactly the kind of flaky
        # magic-number timing this policy warns against.
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            if marker.exists() and marker.read_text().strip() not in ("", "0"):
                break
            await asyncio.sleep(0.02)
        cancel_event.set()

    canceller = asyncio.create_task(_cancel_once_actually_running())  # keep a reference, avoid GC
    result = await backend.run(
        [sys.executable, "-c", script, str(marker)], policy, cancel_event=cancel_event,
    )
    await canceller
    assert result.cancelled is True, (
        "run() did not report the in-container process as verified-stopped"
    )

    count_at_return = int(marker.read_text())
    await asyncio.sleep(0.5)  # if the loop is still alive, it will have kept writing
    count_after_wait = int(marker.read_text())
    assert count_after_wait == count_at_return, (
        f"the child kept running after cancel ({count_at_return} -> {count_after_wait}) — "
        "the host-side client was stopped but the real workload was not (#3862's own bug shape)"
    )


@pytest.mark.asyncio
async def test_docker_kill_in_container_reports_false_when_the_pidfile_is_missing(
    fake_docker_bin: str, tmp_path: Path,
) -> None:
    """Tier 2: #3862 — a missing pidfile (workload exited before writing it,
    or the container is already gone) is reported as "could not verify"
    (False), never raised — cancellation must not raise past this point."""
    missing = str(tmp_path / "no-such-pidfile")
    verified = await _docker_kill_in_container(fake_docker_bin, "unused", missing)
    assert verified is False


@pytest.mark.asyncio
async def test_docker_kill_in_container_verifies_a_real_process_is_gone(
    fake_docker_bin: str, tmp_path: Path,
) -> None:
    """Tier 2: #3862 — direct witness of _docker_kill_in_container's own
    contract: given a pidfile pointing at a REAL running process, it signals
    it and returns True only once a REAL liveness check confirms it's gone
    (not merely "a signal was sent")."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
    )
    pidfile = tmp_path / "real.pid"
    pidfile.write_text(str(proc.pid))

    assert os.kill(proc.pid, 0) is None  # alive before

    verified = await _docker_kill_in_container(fake_docker_bin, "unused", str(pidfile))
    assert verified is True

    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)  # gone after — the real OS-level check, not a re-derivation

    await proc.wait()
