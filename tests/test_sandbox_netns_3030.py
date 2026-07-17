"""Tier 2/2c: Linux network-namespace isolation for `policy.network=False` (#3030).

#3030 measured that `network: false` was silently unenforced whenever
`allow_subprocess: true` (the stdio-MCP default): the seccomp-BPF filter — the
only thing that ever carried a `network: false` policy on Linux — is skipped
entirely in that case, and even when it loads, it is a syscall-name denylist
that io_uring's `IORING_OP_CONNECT`/`IORING_OP_SEND` never touch. The fix moves
the sandboxed process into a fresh, interface-less Linux network namespace
(`backends.netns.isolate_network_namespace`) before Landlock/seccomp apply —
independent of `allow_subprocess`, and not a syscall-name boundary at all.

Three groups, mirroring `test_landlock_exec_shim_1344e.py`'s shape:
  - pure/structural: the function's own contract (non-Linux raises, a real
    `unshare()` failure raises, mapping failures don't) — no real namespace
    needed, driven with a monkeypatched `_unshare`;
  - real enforcement, where netns is actually available on this host — a real
    outbound connect through the production `wrap_command` seam, for both the
    exec-shim path and the fork+preexec_fn `run()` path;
  - fail-closed: a host that cannot deliver the isolation refuses to run the
    target rather than run it with network reachable.

⚠ The enforcement group SKIPS where netns is unavailable — non-Linux, or a
Linux host with unprivileged user namespaces disabled
(`/proc/sys/kernel/unprivileged_userns_clone=0`). That is every job in
`test.yml` (macOS), so a green run of this file on a dev box witnesses nothing
about enforcement — read the skips. Live-witnessed on a real Linux host
(colima, Ubuntu 24.04, kernel 6.8.0-100, aarch64): a real outbound
`connect()`+`send()` and a real `io_uring` `IORING_OP_CONNECT` (via the
`liburing` bindings) BOTH succeeded pre-fix under `network=False,
allow_subprocess=True`, and BOTH fail with `ENETUNREACH` post-fix. Stripping
the `isolate_network_namespace()` call back out reproduced the pre-fix exfil
exactly (strip-falsify), confirming this fix — not something else — closes it.

No mocks — the real SandboxPolicy / real production entry points / a real
subprocess. `monkeypatch` is used only to substitute this module's OWN
`_unshare` wrapper (a thin ctypes call, not a collaborator) to deterministically
exercise the failure branch without depending on a specific host's kernel
config, mirroring `test_sandbox_seccomp.py`'s `monkeypatch.setattr("platform.system", ...)`
pattern for the same reason.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from reyn.security.sandbox.backends.netns import (
    _CLONE_NEWNET,
    _CLONE_NEWUSER,
    isolate_network_namespace,
    netns_available,
)
from reyn.security.sandbox.policy import SandboxPolicy


def _reset_netns_cache() -> None:
    import reyn.security.sandbox.backends.netns as netns_mod

    netns_mod._AVAILABLE = None


# ── pure/structural: the function's own contract ──────────────────────────────


def test_isolate_raises_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: isolate_network_namespace() refuses on a non-Linux platform
    rather than silently doing nothing — a caller that does not check the
    platform first must still fail closed."""
    import reyn.security.sandbox.backends.netns as netns_mod

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    with pytest.raises(RuntimeError, match="Linux-only"):
        isolate_network_namespace()
    assert netns_mod  # module imported cleanly on macOS too


def test_isolate_raises_when_unshare_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: a real unshare() failure (e.g. unprivileged userns disabled)
    raises RuntimeError with the errno detail — this is the fail-closed
    signal the shim and `_child_preexec` depend on.

    `_unshare` is THIS module's own thin ctypes wrapper (not a collaborator
    being faked) substituted with a plain function that raises the same
    OSError shape a real EPERM would, so the test does not depend on which
    kernel knob actually disables unprivileged namespaces on a given host.
    """
    import reyn.security.sandbox.backends.netns as netns_mod

    monkeypatch.setattr("platform.system", lambda: "Linux")

    def _fail(flags: int) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(netns_mod, "_unshare", _fail)
    with pytest.raises(RuntimeError, match="unshare.*failed"):
        isolate_network_namespace()


def test_isolate_requests_both_clone_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: the unshare() call combines CLONE_NEWUSER and CLONE_NEWNET in
    ONE call — an unprivileged process cannot CLONE_NEWNET alone (needs
    CAP_NET_ADMIN in the current user namespace), so a caller must never end
    up with a fresh user namespace but no fresh net namespace or vice versa."""
    import reyn.security.sandbox.backends.netns as netns_mod

    monkeypatch.setattr("platform.system", lambda: "Linux")
    seen: list[int] = []

    def _record(flags: int) -> None:
        seen.append(flags)

    monkeypatch.setattr(netns_mod, "_unshare", _record)
    # /proc/self/{setgroups,uid_map,gid_map} writes will fail in-process (this
    # test process is not actually in a fresh userns) — that is the documented
    # best-effort path, not an error, so no exception is expected here.
    isolate_network_namespace()
    assert seen == [_CLONE_NEWUSER | _CLONE_NEWNET]


def test_mapping_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: a failure writing /proc/self/{uid,gid}_map (measured on real
    Ubuntu 24.04 hosts with AppArmor's unprivileged-userns restriction — the
    unshare() itself succeeds but these writes are refused) must NOT raise.
    Network isolation (the property this module exists for) is already in
    effect once unshare() returns; identity-map fidelity is best-effort."""
    import reyn.security.sandbox.backends.netns as netns_mod

    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(netns_mod, "_unshare", lambda flags: None)

    real_open = open

    def _blocked_open(path, mode="r", *a, **kw):  # noqa: ANN001
        if str(path).startswith("/proc/self/") and "w" in mode:
            raise PermissionError(1, "Operation not permitted")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", _blocked_open)
    isolate_network_namespace()  # must not raise


# ── real enforcement, where netns is actually available on this host ─────────

requires_netns = pytest.mark.skipif(
    not netns_available(),
    reason="network-namespace isolation unavailable on this host — real "
    "enforcement cannot be witnessed (non-Linux, or unprivileged user "
    "namespaces disabled)",
)

_EXFIL_PROBE = """
import socket
try:
    s = socket.create_connection(("8.8.8.8", 53), timeout=3)
    s.sendall(b"exfil")
    print("EXFIL_SUCCEEDED")
except OSError as e:
    print("EXFIL_BLOCKED", e)
"""


def _run_through_shim(policy: SandboxPolicy, code: str) -> subprocess.CompletedProcess:
    """Launch python -c *code* through the real Landlock wrap (the
    `landlock_exec` re-exec shim) — the production MCP-stdio seam."""
    import os
    from pathlib import Path

    import reyn
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    wrapped = LandlockBackend().wrap_command([sys.executable, "-c", code], policy)
    src_root = Path(reyn.__file__).resolve().parent.parent
    return subprocess.run(
        wrapped.argv,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(src_root)},
    )


@requires_netns
def test_shim_blocks_outbound_connect_when_network_false_and_subprocess_allowed(
    tmp_path,
) -> None:
    """Tier 2c: the #3030 defect itself — a real outbound connect+send is
    REFUSED under `network=False, allow_subprocess=True` (the stdio-MCP
    default), where it previously SUCCEEDED because the whole seccomp filter
    (the only prior network gate) is skipped in that case."""
    policy = SandboxPolicy(
        write_paths=[str(tmp_path)], read_deny_paths=[], network=False,
        allow_subprocess=True,
    )
    proc = _run_through_shim(policy, _EXFIL_PROBE)
    assert "EXFIL_SUCCEEDED" not in proc.stdout, (
        f"outbound connect succeeded under network=False, allow_subprocess=True "
        f"— the netns boundary did not fire (rc={proc.returncode}, "
        f"stdout={proc.stdout!r}, stderr={proc.stderr[-300:]!r})"
    )
    assert "EXFIL_BLOCKED" in proc.stdout


@requires_netns
def test_shim_positive_control_network_true_still_connects(tmp_path) -> None:
    """Tier 2c: positive control — `network=True` must still allow the same
    connect. Without this, a passing deny above could equally mean the probe
    itself is broken (e.g. no route to the test address from this CI host),
    not that the sandbox denied anything."""
    policy = SandboxPolicy(
        write_paths=[str(tmp_path)], read_deny_paths=[], network=True,
        allow_subprocess=True,
    )
    proc = _run_through_shim(policy, _EXFIL_PROBE)
    assert "EXFIL_SUCCEEDED" in proc.stdout, (
        f"outbound connect failed even with network=True — the probe cannot "
        f"observe a deny through this host at all (rc={proc.returncode}, "
        f"stdout={proc.stdout!r}, stderr={proc.stderr[-300:]!r})"
    )


@requires_netns
def test_shim_non_networking_command_still_runs_under_network_false(tmp_path) -> None:
    """Tier 2c: the non-networking control (#3034 co-vet's arm 2 analogue) — a
    command that makes NO socket call must still RUN under `network=False`.
    Without this, a netns that broke the process wholesale would be
    indistinguishable from one that refused exactly network."""
    touch = shutil.which("touch")
    assert touch, "no touch(1) on PATH"
    marker = tmp_path / "alive"
    policy = SandboxPolicy(
        write_paths=[str(tmp_path)], read_deny_paths=[], network=False,
        allow_subprocess=True,
    )
    proc = _run_through_shim(policy, "")
    _run_through_shim(policy, f"import subprocess; subprocess.run([{touch!r}, {str(marker)!r}])")
    assert marker.exists(), (
        f"under network=False the shim could not run even a non-networking "
        f"command — something is failing wholesale rather than denying "
        f"network specifically (rc={proc.returncode}, stderr={proc.stderr[-300:]!r})"
    )


@pytest.mark.skipif(
    not netns_available() or shutil.which("python3") is None,
    reason="netns unavailable, or no interpreter for a fresh io_uring check",
)
def test_shim_blocks_io_uring_connect_too(tmp_path) -> None:
    """Tier 2c: the io_uring hole a syscall-name denylist would have missed —
    `IORING_OP_CONNECT` never calls `connect()` as a syscall, so a denylist
    mirroring `_NETWORK_SYSCALLS` cannot refuse it. The netns boundary is
    below all syscall entry points (no interface to route to), so it refuses
    this path too. Requires the `liburing` PyPI bindings; skips (does not
    fail) if unavailable rather than asserting a positive control that cannot
    run.
    """
    try:
        import liburing  # noqa: F401
    except ImportError:
        pytest.skip("liburing bindings not installed — cannot drive io_uring directly")

    io_uring_probe = """
import liburing, socket, os
ring = liburing.io_uring()
liburing.io_uring_queue_init(8, ring, 0)
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
addr = liburing.sockaddr(socket.AF_INET, b"8.8.8.8", 53)
sqe = liburing.io_uring_get_sqe(ring)
liburing.io_uring_prep_connect(sqe, sock.fileno(), addr)
liburing.io_uring_submit(ring)
cqe = liburing.io_uring_cqe()
liburing.io_uring_wait_cqe(ring, cqe)
print("IO_URING_EXFIL_SUCCEEDED" if cqe.res == 0 else f"IO_URING_BLOCKED {-cqe.res}")
liburing.io_uring_cqe_seen(ring, cqe)
"""
    policy = SandboxPolicy(
        write_paths=[str(tmp_path)], read_deny_paths=[], network=False,
        allow_subprocess=True,
    )
    proc = _run_through_shim(policy, io_uring_probe)
    assert "IO_URING_EXFIL_SUCCEEDED" not in proc.stdout, (
        f"io_uring connect succeeded under network=False — a syscall-name "
        f"denylist would have missed exactly this path (rc={proc.returncode}, "
        f"stdout={proc.stdout!r}, stderr={proc.stderr[-300:]!r})"
    )


@requires_netns
def test_run_fork_path_also_blocks_outbound_connect(tmp_path) -> None:
    """Tier 2c: the OTHER production seam — LandlockBackend.run()'s
    fork+preexec_fn path (used by the one-shot sandboxed_exec op), as opposed
    to the exec-shim above (used by persistent stdio MCP servers). #3030's own
    `_child_preexec` carries the identical gate as the shim's
    `_apply_seccomp`, so this path needed the same fix verified separately."""
    import asyncio

    from reyn.security.sandbox.backends.landlock import LandlockBackend

    async def _go():
        backend = LandlockBackend()
        policy = SandboxPolicy(
            write_paths=[str(tmp_path)], read_deny_paths=[], network=False,
            allow_subprocess=True,
        )
        return await backend.run([sys.executable, "-c", _EXFIL_PROBE], policy)

    result = asyncio.run(_go())
    stdout = result.stdout.decode()
    assert "EXFIL_SUCCEEDED" not in stdout, (
        f"run()'s fork+preexec_fn path let a real connect through under "
        f"network=False (rc={result.returncode}, stdout={stdout!r}, "
        f"stderr={result.stderr.decode()[-300:]!r})"
    )
    assert "EXFIL_BLOCKED" in stdout


# ── fail-closed: a host that cannot isolate must refuse to run ───────────────


def test_shim_fails_closed_when_netns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: with unshare() forced to fail (simulating a host with
    unprivileged user namespaces disabled), `_apply_landlock` raises rather
    than exec'ing the target with network reachable — this is the fail-closed
    half of #3030's fix, and it must hold even on a host where netns normally
    works, since the failure mode being tested is host-independent by
    construction (a monkeypatched `_unshare`, not a real disabled kernel
    knob)."""
    import reyn.security.sandbox.backends.netns as netns_mod
    from reyn.security.sandbox.landlock_exec import _apply_landlock

    def _fail(flags: int) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(netns_mod, "_unshare", _fail)

    from reyn.security.sandbox.backends.landlock import LandlockBackend

    if not LandlockBackend().available():
        pytest.skip("Landlock unavailable — this test needs it reachable to assert past it")

    policy = SandboxPolicy(write_paths=["/tmp"], read_deny_paths=[], network=False)
    with pytest.raises(RuntimeError, match="unshare.*failed"):
        _apply_landlock(policy)


def test_run_fails_closed_when_netns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: the fork+preexec_fn path also refuses rather than silently
    running with network reachable — surfaced as a clean SandboxResult
    (returncode=-1) rather than an unhandled exception, since Popen wraps a
    preexec_fn failure as a generic `subprocess.SubprocessError` that would
    otherwise propagate uncaught."""
    import asyncio

    import reyn.security.sandbox.backends.netns as netns_mod
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    if not LandlockBackend().available():
        pytest.skip("Landlock unavailable — this test needs it reachable to assert past it")

    def _fail(flags: int) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(netns_mod, "_unshare", _fail)

    async def _go():
        backend = LandlockBackend()
        policy = SandboxPolicy(write_paths=["/tmp"], read_deny_paths=[], network=False)
        return await backend.run(["/bin/echo", "SHOULD-NOT-RUN"], policy)

    result = asyncio.run(_go())
    assert result.returncode == -1
    assert b"SHOULD-NOT-RUN" not in result.stdout
    assert b"network" in result.stderr.lower() or b"3030" in result.stderr
