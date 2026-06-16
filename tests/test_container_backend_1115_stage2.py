"""Tier 2: FP-0008 #1115 Stage 2 — DockerEnvironmentBackend (dual-Protocol, bridge-free).

The container backend implements BOTH EnvironmentBackend (repo FS) and
SandboxBackend (exec), executing FS ops as in-container ``python3 -c`` so the
container reproduces HostBackend's exact Python semantics. ``run()`` is a
``docker exec`` into a login shell (so the image's env-activation is honored) —
NO host-diff bridge.

These tests use a real **local** runner Fake (not a mock): it strips the
``docker exec`` prefix and runs the SAME ``python3 -c <script> <args>`` on the
LOCAL interpreter against a real tmp filesystem. So the actual in-container
snippets are exercised end-to-end (Python-semantics parity), just without a
live Docker daemon.

Pins:
  (a) the backend satisfies BOTH Protocols (EnvironmentBackend + SandboxBackend);
  (b) every FS op produces the SAME result as HostBackend (semantics parity);
  (c) run() is a `docker exec -w <repo_dir> <container> bash -lc 'exec "$@"' …
      <argv>` (login-shell, env-activation honored) with NO diff/reset/apply
      bridge steps, and argv pass through as positional params (no injection);
  (d) FS args are passed via argv (path with shell-special chars round-trips).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from reyn.environment.backend import EnvironmentBackend
from reyn.environment.container_backend import DockerEnvironmentBackend
from reyn.environment.host_backend import HostBackend
from reyn.security.sandbox.backend import SandboxBackend, SandboxResult
from reyn.security.sandbox.policy import SandboxPolicy


def _local_docker_runner(argv, *, stdin=None, timeout=None) -> SandboxResult:
    """Fake: strip the `docker exec … python3 -c SCRIPT ARGS` prefix and run
    SCRIPT locally on the test interpreter against the real (tmp) filesystem."""
    i = argv.index("-c")
    local = [sys.executable, "-c", argv[i + 1], *argv[i + 2:]]
    completed = subprocess.run(local, input=stdin, capture_output=True, check=False)
    return SandboxResult(
        returncode=completed.returncode,
        stdout=completed.stdout or b"",
        stderr=completed.stderr or b"",
    )


def _backend(repo_dir: Path) -> DockerEnvironmentBackend:
    return DockerEnvironmentBackend(
        container="testc", repo_dir=str(repo_dir), fs_runner=_local_docker_runner,
    )


def test_satisfies_both_protocols(tmp_path: Path) -> None:
    """Tier 2: (a) one class is both an EnvironmentBackend and a SandboxBackend."""
    be = _backend(tmp_path)
    assert isinstance(be, EnvironmentBackend)
    assert isinstance(be, SandboxBackend)
    assert be.name == "docker"


def test_fs_ops_match_host_backend_semantics(tmp_path: Path) -> None:
    """Tier 2: (b) every FS op behaves identically to HostBackend.

    Run the same op on HostBackend (real local FS) and the container backend
    (in-container snippet, also hitting the real local FS via the Fake runner)
    and assert equal results — the Python-semantics parity guarantee.
    """
    host = HostBackend()
    cont = _backend(tmp_path)

    f = tmp_path / "dir" / "a.txt"

    # read (missing) → None on both
    assert host.read_bytes(f) is None
    assert cont.read_bytes(f) is None

    # write → read round-trip identical
    cont.write_bytes(f, "héllo-本文\n".encode())
    assert cont.read_bytes(f) == host.read_bytes(f) == "héllo-本文\n".encode()

    # stat parity (size / flags / mode / mtime)
    assert cont.stat(f) == host.stat(f)
    assert cont.stat(tmp_path / "missing") is host.stat(tmp_path / "missing")  # both None

    # mkdir: created / exists / notdir parity
    d = tmp_path / "newdir"
    assert cont.mkdir(d) is True
    assert host.mkdir(d) is False          # already exists as dir
    assert cont.mkdir(d) is False
    with pytest.raises(FileExistsError):
        cont.mkdir(f)                       # a file sits there

    # glob parity (relative under root)
    assert sorted(str(p) for p in cont.glob("**/*.txt", root=tmp_path)) == \
        sorted(str(p) for p in host.glob("**/*.txt", root=tmp_path))

    # move parity
    dst = tmp_path / "moved" / "b.txt"
    assert cont.move(f, dst) is True
    assert cont.read_bytes(dst) == "héllo-本文\n".encode()
    assert cont.move(tmp_path / "nope", dst) is False

    # delete parity
    assert cont.delete(dst) is True
    assert cont.delete(dst) is False


def test_grep_matches_host_backend(tmp_path: Path) -> None:
    """Tier 2: (b) grep parity — same matches as HostBackend (Python re in-container)."""
    host = HostBackend()
    cont = _backend(tmp_path)
    (tmp_path / "x.py").write_text("alpha\nNEEDLE here\nbeta\n", encoding="utf-8")
    (tmp_path / "y.py").write_text("nothing\n", encoding="utf-8")

    rx = re.compile("NEEDLE")
    h = host.grep(tmp_path, rx, output_mode="files_with_matches")
    c = cont.grep(tmp_path, rx, output_mode="files_with_matches")
    assert sorted(str(p) for p in c.files) == sorted(str(p) for p in h.files)

    hc = host.grep(tmp_path, rx)
    cc = cont.grep(tmp_path, rx)
    [hhit] = hc.matches
    [chit] = cc.matches
    assert chit["line_number"] == hhit["line_number"] == 2
    assert chit["content"] == hhit["content"]


def test_fs_arg_with_special_chars_roundtrips(tmp_path: Path) -> None:
    """Tier 2: (d) a path with shell-special chars survives (argv-passed, not interpolated)."""
    cont = _backend(tmp_path)
    weird = tmp_path / "a b$x;'q.txt"
    cont.write_bytes(weird, b"ok")
    assert cont.read_bytes(weird) == b"ok"


def test_run_is_login_shell_docker_exec_no_bridge(tmp_path: Path) -> None:
    """Tier 2: (c) run() = `docker exec -w repo_dir container` into a LOGIN shell
    (`bash -lc 'exec "$@"' …`) so the image's env-activation is honored, no bridge.

    A plain `docker exec <argv>` uses the base PATH only and misses login-shell
    tooling (e.g. a SWE-bench image's `conda activate`-d pytest). The argv are
    passed as positional params after the script — NOT spliced into the script
    text — so there is no shell-injection / quoting surface.
    """
    calls: list[list[str]] = []

    async def _record_runner(argv, *, stdin=None, timeout=None) -> SandboxResult:
        calls.append(argv)
        return SandboxResult(returncode=0, stdout=b"out", stderr=b"")

    import asyncio

    be = DockerEnvironmentBackend(
        container="testc", repo_dir="/testbed", runner=_record_runner,
    )
    res = asyncio.run(be.run(["pytest", "-x"], SandboxPolicy(timeout_seconds=30)))

    assert res.returncode == 0 and res.stdout == b"out"
    # exactly one exec call — no host-diff / reset / clean / apply steps
    [argv] = calls
    assert argv == [
        "docker", "exec", "-w", "/testbed", "testc",
        "bash", "-lc", 'exec "$@"', "reyn-exec", "pytest", "-x",
    ]
    joined = " ".join(a for c in calls for a in c)
    for bridge_token in ("diff", "reset", "clean", "apply"):
        assert bridge_token not in joined, f"bridge step {bridge_token!r} must be absent"


def test_run_adds_i_flag_only_when_stdin_provided(tmp_path: Path) -> None:
    """Tier 2: run() builds `docker exec -i` when stdin is provided, so the
    in-container process receives it — the python-step harness reads its JSON
    request on stdin, and without `-i` docker exec drops the host-piped stdin so
    the harness sees EOF ("harness received empty stdin"; the #183 re-smoke bug).
    Falsification pair: with no stdin (sandboxed_exec) there is no `-i` (unchanged).

    The fake-backend unit (test_python_step_os_sandbox_1352b) recorded a backend
    that never docker-execs, so it could not catch this argv-construction gap —
    the real e2e re-smoke did; this pins it.
    """
    calls: list[list[str]] = []

    async def _record_runner(argv, *, stdin=None, timeout=None) -> SandboxResult:
        calls.append(argv)
        return SandboxResult(returncode=0, stdout=b"ok", stderr=b"")

    import asyncio

    be = DockerEnvironmentBackend(
        container="testc", repo_dir="/testbed", runner=_record_runner,
    )

    # with stdin → `-i` immediately after "exec" (so docker forwards stdin)
    asyncio.run(
        be.run(
            ["python", "-m", "reyn.kernel._python_harness"],
            SandboxPolicy(timeout_seconds=30),
            stdin=b'{"req": 1}',
        )
    )
    [with_stdin] = calls
    assert with_stdin[:3] == ["docker", "exec", "-i"], (
        f"a stdin-carrying exec must use `docker exec -i`: {with_stdin}"
    )

    # falsification: no stdin → no `-i` (sandboxed_exec path unchanged)
    calls.clear()
    asyncio.run(be.run(["pytest", "-x"], SandboxPolicy(timeout_seconds=30)))
    [no_stdin] = calls
    assert "-i" not in no_stdin, f"a stdin-less exec must NOT use `-i`: {no_stdin}"


def test_run_argv_passed_as_positional_params_not_interpolated(tmp_path: Path) -> None:
    """Tier 2: (b/argv-safe) shell-special chars in argv survive verbatim — the
    login-shell wrapper forwards them as positional params, never interpolated.

    The exact argv (including a token with `$`, `;`, spaces, quotes) must appear
    as trailing list elements after the fixed `bash -lc 'exec "$@"' reyn-exec`
    prefix, proving no quoting/injection seam was opened by the login-shell wrap.
    """
    calls: list[list[str]] = []

    async def _record_runner(argv, *, stdin=None, timeout=None) -> SandboxResult:
        calls.append(argv)
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")

    import asyncio

    be = DockerEnvironmentBackend(
        container="testc", repo_dir="/testbed", runner=_record_runner,
    )
    hostile = ["python", "-c", "print('a; rm -rf $HOME')", "x y", '"q"']
    asyncio.run(be.run(hostile, SandboxPolicy(timeout_seconds=30)))

    [argv] = calls
    prefix = ["docker", "exec", "-w", "/testbed", "testc",
              "bash", "-lc", 'exec "$@"', "reyn-exec"]
    assert argv[: len(prefix)] == prefix
    # argv survives byte-for-byte as positional params (no escaping mutation)
    assert argv[len(prefix):] == hostile
