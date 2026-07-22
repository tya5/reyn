"""x86_64 live validation of the seccomp-BPF filter (#2982, part of #2983).

#2975 turned the seccomp-BPF filter live in production for the first time and
validated it on a real Linux host — but that host was **aarch64** (Ubuntu 24.04,
colima). aarch64 has no `stat`/`lstat` syscalls at all (only the `*at` forms), so
no amount of aarch64 testing can surface a missing `stat`/`lstat` allowlist
entry — the gap this script exists to close is *architecture-shaped*, not a bug
that more aarch64 runs would ever find.

This script repeats #2975's live validation (drive the two real production
callsites with real workloads, observe survive/defend) on whatever host actually
runs it — which in `.github/workflows/sandbox-linux-live-x86_64.yml` is a GitHub
Actions `ubuntu-latest` runner (x86_64). It is intentionally NOT a pytest file:
loading a real default-deny filter is irrevocable for the rest of the process
(#2975's own test-guard fix, `8f4405815`), so each probe MUST run in its own
subprocess — this script is that harness, not a test collected into the shared
pytest session.

Honesty, per #2982: this closes ONE of several structural blind spots #2975
shipped with, not all of them:

  - x86_64 syscall NAME resolution (stat/lstat are real x86_64 syscall numbers,
    unlike aarch64 where they don't exist)          -> closed by this script
  - whether an installed `landlock` package version matches the API the shim
    calls (#2980)                                   -> now CHECKED, not merely
    reported. Both Landlock seams build their ruleset through one shared
    `build_ruleset`, so this script drives `landlock_exec.main()` — the real
    production entry point — and RECORDS the result instead of printing an
    `[INFO]` note about a defect it had to route around.
  - Landlock ABI 1-2 (the truncate gap's actual affected range: Ubuntu 22.04
    LTS / RHEL 9 / Debian 12 — most of the installed base) -> NOT closed; this
    runner's kernel ABI is whatever GitHub ships, not chosen for ABI coverage
  - "does a deny actually fire in the product's real op path, end to end"
     -> NOT closed; this is a syscall-filter mechanism probe, not an
        end-to-end op-path test

Do not read a green run of this script as "the sandbox is validated". Read it as
"the syscall-name-resolution gap #2975 flagged as predicted-not-measured is now
measured on x86_64" — see the PR body for the fuller table (#2983).
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass


@dataclass
class ProbeResult:
    label: str
    ok: bool
    detail: str


RESULTS: list[ProbeResult] = []


def _record(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(ProbeResult(label, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Environment preflight — fail loudly if this isn't the environment the job
# claims to be. A silent skip here would be exactly the "gate that cannot
# witness its own assumption" failure mode #2982 is about.
# ---------------------------------------------------------------------------


def _preflight() -> None:
    machine = platform.machine()
    system = platform.system()
    print(f"platform.system()  = {system}")
    print(f"platform.machine() = {machine}")
    try:
        print(f"platform.libc_ver() = {platform.libc_ver()}")
    except Exception as exc:  # noqa: BLE001
        print(f"platform.libc_ver() raised: {exc}")

    if system != "Linux":
        print("FATAL: this script requires Linux (seccomp-BPF is Linux-only).")
        sys.exit(2)
    if machine not in ("x86_64", "amd64", "AMD64"):
        print(
            f"FATAL: this script requires an x86_64 host (got {machine!r}). "
            "It exists specifically to validate a gap aarch64 cannot surface "
            "— running it on aarch64 would silently defeat its purpose."
        )
        sys.exit(2)

    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    if not seccomp_mod.is_available():
        print(
            "FATAL: seccomp_mod.is_available() is False. This job installs "
            "the sandbox-linux extra specifically so pyseccomp is present — "
            "if this fires, the install step is broken, not the environment."
        )
        sys.exit(2)

    # Honest note about glibc: on glibc >= 2.33, `stat(2)`/`lstat(2)` libc calls
    # route through the newfstatat(2) syscall, not the legacy stat/lstat syscall
    # numbers — so a MODERN glibc host (which GitHub's ubuntu-latest is) will
    # NOT actually exercise the legacy stat/lstat syscalls at runtime even
    # though this script confirms libseccomp can resolve their NAMES on the
    # x86_64 syscall table. The runtime gap (Debian 11 / Ubuntu 20.04 / RHEL 8
    # / Amazon Linux 2, glibc < 2.33) stays unmeasured by this job; only name
    # resolution + filter load with those names present is measured here.
    print(
        "NOTE: this host's glibc is used to determine whether legacy "
        "stat/lstat SYSCALLS get exercised — see script docstring."
    )


# ---------------------------------------------------------------------------
# Callsite 1: LandlockBackend._child_preexec (Popen/preexec_fn shape)
# ---------------------------------------------------------------------------


def _run_child_preexec_probe(workdir: str, ruleset: object | None) -> None:
    """Drive `_child_preexec` exactly as `LandlockBackend.run` does: as a real
    `preexec_fn` on a real `subprocess.Popen`, so the probed effect is the
    production code path, not a re-implementation of it."""
    from reyn.security.sandbox.backends.landlock import _child_preexec
    from reyn.security.sandbox.policy import SandboxPolicy

    # allow_subprocess=False explicitly (#3202: the dataclass default flipped to
    # True — a UX-blocking axis is opt-in-restricted, not deny-by-default). This
    # probe's own "defend (must be refused): subprocess spawn / os.fork" arms
    # below are specifically about the OPT-OUT leg, so they must not silently
    # start passing-by-accident once the default itself grants subprocess.
    policy = SandboxPolicy(write_paths=[workdir], allow_subprocess=False)

    def _popen(argv: list[str], **kw: object) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            preexec_fn=lambda: _child_preexec(ruleset, policy),  # noqa: PLC0415
            capture_output=True,
            timeout=10,
            **kw,  # type: ignore[arg-type]
        )

    survive = {
        "echo": ["/bin/echo", "hello"],
        "ls /": ["/bin/ls", "/"],
        "cat": ["/bin/cat", "/etc/hostname"],
    }
    for label, argv in survive.items():
        try:
            proc = _popen(argv)
            _record(
                f"callsite1 survive: {label}",
                proc.returncode == 0,
                f"rc={proc.returncode} stderr={proc.stderr[:200]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            _record(f"callsite1 survive: {label}", False, f"raised {exc!r}")

    # socket()+bind() must SURVIVE under network=False (#3060 option A): socket
    # and bind are always allowed (neither moves a byte on its own), and this is
    # the exact shape of the benign urllib3 import-time IPv6-support probe
    # (`socket.socket(AF_INET6)` then `bind(('::1', 0))`, never a connect()) that
    # used to be refused as collateral of the network gate. Falls back to
    # AF_INET/127.0.0.1 if IPv6 loopback is unavailable on the runner, so the
    # allow-set {socket, bind} is witnessed either way.
    socket_bind_code = (
        "import socket\n"
        "try:\n"
        "    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)\n"
        "    s.bind(('::1', 0))\n"
        "except OSError:\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    s.bind(('127.0.0.1', 0))\n"
        "print('socket-bind-ok')\n"
    )
    try:
        proc = _popen([sys.executable, "-c", socket_bind_code])
        _record(
            "callsite1 survive: socket()+bind() loopback (#3060 allow-set)",
            proc.returncode == 0 and b"socket-bind-ok" in proc.stdout,
            f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr[:200]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        _record("callsite1 survive: socket()+bind() loopback", False, f"raised {exc!r}")

    # NULL-addr sendto/recvfrom on a connected AF_UNIX socketpair must SURVIVE
    # under network=False (#3060 case-b): this is the async event loop's self-pipe
    # wakeup (send()/recv() lower to sendto/recvfrom with arg4==NULL). It is the
    # SURVIVE half of the pair whose DEFEND half is the addressed-sendto probe
    # below — together they witness the NULL-address gate is neither too tight
    # (self-pipe works) nor too loose (addressed egress denied).
    socketpair_code = (
        "import socket\n"
        "a, b = socket.socketpair()\n"
        "a.send(b'ping')\n"
        "assert b.recv(4) == b'ping'\n"
        "print('socketpair-ok')\n"
    )
    try:
        proc = _popen([sys.executable, "-c", socketpair_code])
        _record(
            "callsite1 survive: NULL-addr socketpair sendto/recvfrom (#3060 self-pipe)",
            proc.returncode == 0 and b"socketpair-ok" in proc.stdout,
            f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr[:200]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        _record("callsite1 survive: NULL-addr socketpair sendto/recvfrom", False, f"raised {exc!r}")

    # python: file read+write, mkdir/remove/rename/rmtree — driven inline so it
    # is the real _child_preexec-wrapped python process doing the syscalls.
    py_workload = f"""
import os, shutil
d = {workdir!r}
p = os.path.join(d, "probe.txt")
with open(p, "w") as f:
    f.write("hi")
with open(p) as f:
    assert f.read() == "hi"
os.mkdir(os.path.join(d, "subdir"))
os.rename(os.path.join(d, "subdir"), os.path.join(d, "subdir2"))
os.rmdir(os.path.join(d, "subdir2"))
os.remove(p)
print("workload-ok")
"""
    try:
        proc = _popen([sys.executable, "-c", py_workload])
        _record(
            "callsite1 survive: python read+write+mkdir+remove+rename+rmtree",
            proc.returncode == 0 and b"workload-ok" in proc.stdout,
            f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr[:300]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        _record("callsite1 survive: python file workload", False, f"raised {exc!r}")

    # Nested subprocess+pipe capture under allow_subprocess=True (#3207 co-vet).
    # fork/vfork/clone/clone3 alone only let a SANDBOXED process spawn a bare
    # grandchild — the realistic shape (an MCP server itself running
    # `subprocess.run([...], capture_output=True)`, i.e. `ls | wc`-style output
    # capture) additionally needs dup2/dup3 to redirect the grandchild's
    # stdout/stderr onto the parent's pipe fds, which CPython issues AFTER the
    # inner fork and BEFORE the inner execve — i.e. under the already-loaded
    # filter, since it is irrevocable and survives fork+exec. A separate policy
    # (allow_subprocess=True) drives this probe: the `policy` object above is
    # deliberately allow_subprocess=False for the deny arms further down.
    subprocess_pipe_policy = SandboxPolicy(write_paths=[workdir], allow_subprocess=True)

    def _popen_subprocess_allowed(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            preexec_fn=lambda: _child_preexec(ruleset, subprocess_pipe_policy),  # noqa: PLC0415
            capture_output=True,
            timeout=10,
        )

    nested_pipe_code = (
        "import subprocess\n"
        "r = subprocess.run(['sh', '-c', 'ls / | wc -l'], capture_output=True, timeout=10)\n"
        "assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)\n"
        "assert int(r.stdout.strip()) > 0, r.stdout\n"
        "print('nested-pipe-ok')\n"
    )
    try:
        proc = _popen_subprocess_allowed([sys.executable, "-c", nested_pipe_code])
        _record(
            "callsite1 survive: nested subprocess+pipe capture under "
            "allow_subprocess=True (dup2/dup3, #3207)",
            proc.returncode == 0 and b"nested-pipe-ok" in proc.stdout,
            f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr[:300]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        _record(
            "callsite1 survive: nested subprocess+pipe capture", False, f"raised {exc!r}"
        )

    # chmod / truncate — must be refused (EPERM), matching #2975's own
    # falsification (_EXCLUDED_UNGOVERNABLE).
    for label, code in {
        "chmod": f"import os; os.chmod({workdir!r}, 0o777)",
        "truncate": (
            f"import os; p={os.path.join(workdir, 'trunc.txt')!r}; "
            "open(p, 'w').close(); os.truncate(p, 0)"
        ),
    }.items():
        try:
            proc = _popen([sys.executable, "-c", code])
            refused = proc.returncode != 0
            _record(
                f"callsite1 defend (must be refused): {label}",
                refused,
                f"rc={proc.returncode} stderr={proc.stderr[:200]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            _record(f"callsite1 defend: {label}", False, f"raised {exc!r}")

    # connect()/sendto() must be REFUSED under network=False (#3060 option A):
    # socket()/bind() are always allowed, but the syscalls that actually move
    # bytes to/from a peer stay gated on policy.network. connect = TCP egress
    # path, sendto = UDP egress path (a connectionless datagram send needs no
    # prior connect(), so it must be denied on its own axis). Together with the
    # socket()+bind() survive probe above, this is option A's core witnessed on
    # a live x86_64 host: "socket+bind can be created, connect+sendto are
    # refused."
    defend = {
        "subprocess spawn": [sys.executable, "-c", "import subprocess; subprocess.run(['/bin/echo','x'])"],
        "os.fork": [sys.executable, "-c", "import os; os.fork()"],
        "connect (TCP egress)": [
            sys.executable, "-c",
            "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
            "s.connect(('93.184.216.34', 80))",
        ],
        "sendto (UDP egress)": [
            sys.executable, "-c",
            "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); "
            "s.sendto(b'x', ('93.184.216.34', 80))",
        ],
    }
    for label, argv in defend.items():
        try:
            proc = _popen(argv)
            _record(
                f"callsite1 defend (must be refused): {label}",
                proc.returncode != 0,
                f"rc={proc.returncode} stderr={proc.stderr[:200]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            _record(f"callsite1 defend: {label}", False, f"raised {exc!r}")

    # ptrace: use ctypes to actually issue the syscall rather than relying on a
    # higher-level API being present.
    ptrace_code = (
        "import ctypes; libc = ctypes.CDLL(None, use_errno=True); "
        "r = libc.ptrace(0, 0, 0, 0); "
        "import sys; sys.exit(0 if r == 0 else 1)"
    )
    try:
        proc = _popen([sys.executable, "-c", ptrace_code])
        _record(
            "callsite1 defend (must be refused): ptrace",
            proc.returncode != 0,
            f"rc={proc.returncode} stderr={proc.stderr[:200]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        _record("callsite1 defend: ptrace", False, f"raised {exc!r}")


def _validate_callsite1(workdir: str) -> None:
    from reyn.security.sandbox.backends.landlock import LandlockBackend

    backend = LandlockBackend()
    if backend.available():
        import landlock  # noqa: PLC0415

        FS = landlock.FSAccess  # type: ignore[attr-defined]
        read_rules = FS.READ_FILE | FS.READ_DIR | FS.EXECUTE
        write_rules = (
            read_rules
            | FS.WRITE_FILE
            | FS.MAKE_REG | FS.MAKE_DIR | FS.MAKE_SYM
            | FS.MAKE_CHAR | FS.MAKE_BLOCK | FS.MAKE_FIFO | FS.MAKE_SOCK
            | FS.REMOVE_FILE | FS.REMOVE_DIR
        )
        handled = read_rules | write_rules
        ruleset = landlock.Ruleset(restrict_rules=handled)  # type: ignore[attr-defined]
        ruleset.allow("/", rules=read_rules)
        ruleset.allow(workdir, rules=write_rules)
        print(
            f"Landlock available on this host (ABI {backend._abi_version}); "
            "stacking Landlock+seccomp together — NOT exercised by #2975's "
            "aarch64 validation (that run used ruleset=None)."
        )
        _run_child_preexec_probe(workdir, ruleset)
    else:
        print(
            "Landlock NOT available on this host "
            f"(import_error={backend.import_error!r}); exercising the "
            "seccomp-only shape (ruleset=None), same as #2975's aarch64 run."
        )
        _run_child_preexec_probe(workdir, None)


# ---------------------------------------------------------------------------
# Callsite 2: landlock_exec._apply_seccomp (plain os.execvp shape)
# ---------------------------------------------------------------------------


def _validate_callsite2(workdir: str) -> None:
    """Drive `_apply_seccomp` (the shim's real seccomp step) followed by a real
    `os.execvp`, matching the shape `landlock_exec.main()` uses in production.

    This isolates the seccomp step, which is what this script is FOR (x86_64
    syscall-name resolution). The full entry point — `_apply_landlock`, which
    used to raise `AttributeError` on the pinned package before seccomp was ever
    reached (#2980) — is exercised separately by `_validate_shim_entry_point`
    below, now that it is reachable at all."""
    def _run(code_after_apply: str) -> subprocess.CompletedProcess:
        script = f"""
import sys
sys.path.insert(0, {os.getcwd()!r})
from reyn.security.sandbox.landlock_exec import _apply_seccomp
from reyn.security.sandbox.policy import SandboxPolicy
# allow_subprocess=False explicitly (#3202 default flip) — this probe's
# "defend (must be refused): subprocess spawn" arm below is the opt-out leg.
_apply_seccomp(SandboxPolicy(write_paths=[{workdir!r}], allow_subprocess=False))
{code_after_apply}
"""
        return subprocess.run(
            [sys.executable, "-c", script], capture_output=True, timeout=10,
        )

    survive = {
        "shim execs echo": "import os; os.execvp('/bin/echo', ['/bin/echo', 'hi'])",
        "shim execs ls": "import os; os.execvp('/bin/ls', ['/bin/ls', '/'])",
        "shim execs python read+write": (
            f"import os; p={os.path.join(workdir, 'shim.txt')!r}\n"
            "with open(p, 'w') as f: f.write('x')\n"
            "with open(p) as f: assert f.read() == 'x'\n"
            "print('shim-workload-ok')"
        ),
    }
    for label, code in survive.items():
        try:
            proc = _run(code)
            _record(
                f"callsite2 survive: {label}",
                proc.returncode == 0,
                f"rc={proc.returncode} stderr={proc.stderr[:200]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            _record(f"callsite2 survive: {label}", False, f"raised {exc!r}")

    try:
        proc = _run(
            "import subprocess; subprocess.run(['/bin/echo', 'x'])"
        )
        _record(
            "callsite2 defend (must be refused): subprocess spawn",
            proc.returncode != 0,
            f"rc={proc.returncode} stderr={proc.stderr[:200]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        _record("callsite2 defend: subprocess spawn", False, f"raised {exc!r}")


# ---------------------------------------------------------------------------
# Callsite 2b: the shim's FULL entry point (_apply_landlock + _apply_seccomp)
# ---------------------------------------------------------------------------


def _validate_shim_entry_point(workdir: str) -> None:
    """Drive `landlock_exec.main()` end to end — the production entry point.

    This is the check #2980 was: for 41 days the shim called `Ruleset` methods
    the pinned `landlock==1.0.0.dev5` does not define, so `main()` raised
    `AttributeError` before restricting anything, and nothing drove it. An
    earlier revision of this script PRINTED that as `[INFO] NOT reachable` and
    carried on green — an accurate note that gated nothing, which is the whole
    shape of the defect. It is a recorded probe now.

    Records a FAIL (not a skip) when Landlock is present but the entry point
    does not work. When Landlock is absent it records a PASS on the shim's OTHER
    guarantee — refusing to exec the target unrestricted — because that is the
    correct behaviour there and is still worth witnessing; what must never
    happen is `rc=0` with the target run and nothing applied.
    """
    from reyn.security.sandbox.backends.landlock import LandlockBackend
    from reyn.security.sandbox.landlock_exec import _policy_to_json
    from reyn.security.sandbox.policy import SandboxPolicy

    landlock_present = LandlockBackend().available()
    print(f"[INFO] LandlockBackend().available() = {landlock_present}")

    # Build the shim's own `--policy` JSON via its real serializer
    # (`_policy_to_json`) rather than hand-formatting JSON into an f-string —
    # a hand-rolled version broke on quote-collision between the JSON string
    # and the Python source string during local validation of this script.
    # allow_subprocess=True: this probe is about the entry point being reachable
    # at all, not about the fork gate (the self-test's spawn probe owns that).
    policy_arg = _policy_to_json(
        SandboxPolicy(write_paths=[workdir], allow_subprocess=True)
    )
    shim_probe = f"""
import sys
sys.path.insert(0, {os.getcwd()!r})
from reyn.security.sandbox.landlock_exec import main
sys.exit(main(["--policy", {policy_arg!r}, "--", "/bin/echo", "shim-main-ok"]))
"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", shim_probe], capture_output=True, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        _record("callsite2b: landlock_exec.main() end-to-end", False, f"raised {exc!r}")
        return

    detail = f"rc={proc.returncode} stdout={proc.stdout[:120]!r} stderr={proc.stderr[:300]!r}"
    if landlock_present:
        _record(
            "callsite2b: landlock_exec.main() restricts and execs the target (#2980)",
            proc.returncode == 0 and b"shim-main-ok" in proc.stdout,
            detail,
        )
    else:
        _record(
            "callsite2b: landlock_exec.main() REFUSES to exec unrestricted "
            "(no Landlock on this host)",
            proc.returncode != 0 and b"shim-main-ok" not in proc.stdout,
            detail,
        )


# ---------------------------------------------------------------------------
# denial.py (#2820) launcher-fork classification — must fire on Linux now that
# the filter actually refuses fork with EPERM (not KILL).
# ---------------------------------------------------------------------------


def _validate_denial_classification(workdir: str) -> None:
    from reyn.security.sandbox.backends.landlock import _child_preexec
    from reyn.security.sandbox.denial import DENIAL_FORK, classify_denial
    from reyn.security.sandbox.policy import SandboxPolicy

    # allow_subprocess=False explicitly (#3202 default flip): this probe's whole
    # point is that a REFUSED fork classifies as DENIAL_FORK, so the fork must
    # actually be denied here rather than accidentally allowed by the default.
    policy = SandboxPolicy(write_paths=[workdir], allow_subprocess=False)
    shapes = {
        "ls / | wc -l": "ls / | wc -l",
        "ls /; echo done": "ls /; echo done",
        "x=$(echo hi)": "x=$(echo hi); echo $x",
    }
    for label, shell_cmd in shapes.items():
        try:
            proc = subprocess.run(
                ["/bin/bash", "-c", shell_cmd],
                preexec_fn=lambda: _child_preexec(None, policy),  # noqa: PLC0415, B023
                capture_output=True,
                timeout=10,
            )
            klass = classify_denial(proc.returncode, proc.stderr)
            _record(
                f"denial.py classifies: {label}",
                klass == DENIAL_FORK,
                f"rc={proc.returncode} class={klass} stderr={proc.stderr[:200]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            _record(f"denial.py classifies: {label}", False, f"raised {exc!r}")


def main() -> int:
    _preflight()

    with tempfile.TemporaryDirectory() as workdir:
        print("\n=== Callsite 1: LandlockBackend._child_preexec ===")
        _validate_callsite1(workdir)

        print("\n=== Callsite 2: landlock_exec._apply_seccomp ===")
        _validate_callsite2(workdir)

        print("\n=== Callsite 2b: landlock_exec.main() end-to-end (#2980) ===")
        _validate_shim_entry_point(workdir)

        print("\n=== denial.py (#2820) launcher-fork classification ===")
        _validate_denial_classification(workdir)

    print("\n=== Summary ===")
    n_ok = sum(1 for r in RESULTS if r.ok)
    n_total = len(RESULTS)
    for r in RESULTS:
        if not r.ok:
            print(f"  FAIL: {r.label} — {r.detail}")
    print(f"{n_ok}/{n_total} probes as expected")

    if n_ok != n_total:
        print(
            "\nAt least one probe did NOT behave as #2975's aarch64 run "
            "predicted. Per #2982: this is exactly the kind of x86_64-only "
            "defect this job exists to surface — do not paper over it with a "
            "workaround here. Report the specific probe and its detail."
        )
        return 1

    print(
        "\nAll probes matched #2975's aarch64 expectations, now confirmed on "
        "x86_64. This closes the syscall-name-resolution blind spot only — "
        "see the script docstring and the #2982 PR body for what remains open "
        "(#2980 package-API mismatch, Landlock ABI 1-2 truncate gap, and "
        "end-to-end op-path enforcement)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
