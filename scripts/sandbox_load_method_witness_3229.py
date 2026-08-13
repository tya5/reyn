"""Standing CI gate: the sandbox boundary is LOAD-METHOD INDEPENDENT (#3229,
derived from #3227's competitive research on Claude Code's ld-linux-direct +
mmap-load exec-hook bypass).

**The question this witnesses.** #3227's research found that some sandboxes
key their confinement on *how* a binary is launched — an ``execve``-argv hook
that pattern-matches argv[0], or an LD_PRELOAD interposer wrapping a named
``exec*`` libc symbol. Such a sandbox is bypassed by loading code a different
way: invoking the ELF interpreter (``ld-linux``) directly on a target binary,
or ``mmap``-ing executable code into the process and jumping to it, neither of
which necessarily takes the code path the hook expects.

reyn's boundary is architecturally NOT that shape — Landlock is an LSM hook at
the syscall layer, and the seccomp-BPF filter attaches to the whole process's
syscall table, not to a named libc symbol or a specific argv[0]. Both survive
across whatever code path led to a given syscall being issued. That is a
reasonable architecture READ (see #3229's issue body), but this repo's own
standing rule is that a suspected-good boundary gets a WITNESS, not a
confirmed-by-reading verdict (mirrors ``sandbox_landlock_deny_gate.py``, the
axis-witness lineage at #3030/#3059/#2983).

**What is witnessed, concretely.** Under ONE ``SandboxPolicy`` (write granted
to exactly one temp dir, ``network=False``), for EACH of two load methods —
(a) ``ld-linux.so <target-binary>`` invoked directly as argv[0] (bypasses the
kernel's own PT_INTERP dispatch of the ORIGINAL binary as the exec target —
the interpreter is the thing the kernel exec's, argv[0], not a helper the
target binary chooses), (b) code reached via ``mmap(PROT_EXEC)`` and called
through a raw function pointer, with NO ``exec*`` syscall at all —

  - a write outside the granted path is DENIED (Landlock)
  - a loopback ``connect()`` is DENIED (seccomp network gate)

Four checks total (2 load methods x 2 axes). A regression in either axis
under either load method is exactly the hole #3227 raised as the
"real sandbox breach, separate from #3227's own argv-allowlist" escalation
case — this gate turns "we read the architecture as fine" into a witnessed
green (or a loud red, which is the higher-priority outcome per the issue).

**Why a standalone CI-only script, not a pytest gate (mirrors
``sandbox_landlock_deny_gate.py``).** Landlock/seccomp are Linux-only and
this repo's own ``test.yml`` omits the ``sandbox-linux`` extra from the
shared pytest session (loading a real default-deny filter is irrevocable for
the process that does it — the wrong shape for pytest's shared session). A
pytest test gated on ``@requires_landlock`` would report a SKIP everywhere
except this one job, and per this repo's own axis-witness discipline
(CLAUDE.md, ``docs/deep-dives/contributing/verification-hazards.md``), a
skip is green — exactly the failure mode this witness exists to rule out.
So, like ``sandbox_landlock_deny_gate.py``: every precondition is FATAL, not
skipped, and this script has no green outcome that did not observe all four
checks fire.

**Scope of a green run.** This witnesses ONE Landlock ABI, ONE kernel, ONE
architecture (x86_64 — the shellcode below is hand-assembled machine code for
that ISA specifically, so this gate FATALs rather than silently skipping on
any other ``platform.machine()``). Read a green run as "on this host, on this
ABI, both load methods still hit the same syscall-layer deny" — not as
"the boundary is load-method-independent on every kernel/arch reyn ships on".

**Not a change to production enforcement.** This adds no code path
production calls — CLAUDE.md is explicit that ``enforcement_self_test``
(``src/reyn/security/sandbox/self_test.py``) is the 2-layer PRODUCTION gate
(deny leg only, write + spawn axes only) and widening its blast radius
requires an owner-level decision this issue does not grant. This script
reuses that module's ``_attempt_create`` harness (the same
wrap_command-then-observe-the-filesystem oracle every existing probe uses)
purely as CI-conformance evidence, exactly the ``axis_contract`` /
``test_sandbox_axis_contract_2983.py`` precedent CLAUDE.md names for
"richer per-axis contract, CI-only".
"""
from __future__ import annotations

import platform
import shutil
import socket as _socket_mod
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# The interpreter search order for glibc x86_64 across the distros CI images
# commonly use. FATAL (not skipped) if none exist — see the module docstring.
_LD_LINUX_CANDIDATES = (
    "/lib64/ld-linux-x86-64.so.2",
    "/lib/ld-linux-x86-64.so.2",
    "/usr/lib64/ld-linux-x86-64.so.2",
    "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
)

_PROBE_TIMEOUT_SECONDS = 30


@dataclass
class Check:
    label: str
    ok: bool
    detail: str


CHECKS: list[Check] = []


def _record(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(Check(label, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def _find_ld_linux() -> str:
    for candidate in _LD_LINUX_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    print(
        "FATAL: no glibc x86_64 ld-linux interpreter found among "
        f"{_LD_LINUX_CANDIDATES!r}. This gate cannot witness the ld-linux "
        "load method without it — reporting FATAL rather than skipping the "
        "check and calling the result green."
    )
    sys.exit(2)


# ── (b) mmap-load: hand-assembled x86-64 shellcode that calls a resolved libc
# function pointer directly, with NO exec*/dlopen syscall in the load path at
# all — the code arrives in the process purely via mmap(PROT_EXEC) + a raw
# ctypes function-pointer call. This is the child-process-side generator; it
# runs INSIDE the sandboxed subprocess (built as a `python3 -c <src>` argv),
# never in this gate's own process. ──

_MMAP_OPEN_CHILD_SOURCE = """
import ctypes
import mmap
import struct
import sys

libc = ctypes.CDLL(None, use_errno=True)
open_addr = ctypes.cast(libc.open, ctypes.c_void_p).value

path_buf = ctypes.create_string_buffer({path!r}.encode() + b"\\x00")
path_addr = ctypes.addressof(path_buf)

O_WRONLY, O_CREAT, O_TRUNC = 1, 64, 512
flags = O_WRONLY | O_CREAT | O_TRUNC
mode = 0o644

# movabs rdi, path_addr ; mov esi, flags ; mov edx, mode ;
# movabs rax, open_addr ; call rax ; ret
code = (
    b"\\x48\\xBF" + struct.pack("<Q", path_addr)
    + b"\\xBE" + struct.pack("<i", flags)
    + b"\\xBA" + struct.pack("<i", mode)
    + b"\\x48\\xB8" + struct.pack("<Q", open_addr)
    + b"\\xFF\\xD0\\xC3"
)

mem = mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
mem.write(code)
buf = (ctypes.c_char * len(code)).from_buffer(mem)
func_addr = ctypes.addressof(buf)
fn = ctypes.CFUNCTYPE(ctypes.c_long)(func_addr)
fn()  # oracle is the filesystem (did {path!r} get created), not this return value
sys.exit(0)
"""

_MMAP_CONNECT_CHILD_SOURCE = """
import ctypes
import mmap
import socket
import struct
import sys

libc = ctypes.CDLL(None, use_errno=True)
connect_addr = ctypes.cast(libc.connect, ctypes.c_void_p).value

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # socket() is always-allowed (#3060)
fd = s.fileno()

sockaddr = struct.pack("<H", socket.AF_INET) + struct.pack(">H", {port}) + socket.inet_aton("127.0.0.1") + b"\\x00" * 8
addr_buf = ctypes.create_string_buffer(sockaddr)
addr_addr = ctypes.addressof(addr_buf)

# mov edi, fd ; movabs rsi, addr_addr ; mov edx, 16 ;
# movabs rax, connect_addr ; call rax ; ret
code = (
    b"\\xBF" + struct.pack("<i", fd)
    + b"\\x48\\xBE" + struct.pack("<Q", addr_addr)
    + b"\\xBA" + struct.pack("<i", 16)
    + b"\\x48\\xB8" + struct.pack("<Q", connect_addr)
    + b"\\xFF\\xD0\\xC3"
)

mem = mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
mem.write(code)
buf = (ctypes.c_char * len(code)).from_buffer(mem)
func_addr = ctypes.addressof(buf)
fn = ctypes.CFUNCTYPE(ctypes.c_long)(func_addr)
ret = fn()
if ret == 0:
    open({marker!r}, "w").close()  # only created if connect() actually SUCCEEDED
sys.exit(0)
"""


def _preflight() -> str:
    print("=== Host ===")
    print(f"platform.system()  = {platform.system()}")
    print(f"platform.machine() = {platform.machine()}")
    print(f"platform.release() = {platform.release()}")

    if platform.system() != "Linux":
        print(
            "FATAL: Landlock/seccomp are Linux-only. This gate has no meaning "
            f"on {platform.system()} and must not report one."
        )
        sys.exit(2)

    if platform.machine() not in ("x86_64", "amd64"):
        print(
            "FATAL: the mmap-load shellcode below is hand-assembled x86-64 "
            f"machine code; this runner is {platform.machine()!r}. Reporting "
            "FATAL rather than silently skipping the mmap-load arms and "
            "calling the result green."
        )
        sys.exit(2)

    from reyn.security.sandbox.backends.landlock import LandlockBackend

    backend = LandlockBackend()
    if not backend.available():
        print(
            "FATAL: LandlockBackend().available() is False — the Landlock "
            f"mechanism is not even present (import_error={backend.import_error!r})."
        )
        sys.exit(2)

    import reyn.security.sandbox.backends.seccomp as seccomp_mod

    if not seccomp_mod.is_available():
        print("FATAL: seccomp is not available (pyseccomp absent?).")
        sys.exit(2)

    print(f"\\nLandlock ABI = {backend.abi_version}")
    return _find_ld_linux()


def _write_axis_ld_linux(ld_linux: str, backend) -> None:
    from reyn.security.sandbox.policy import SandboxPolicy
    from reyn.security.sandbox.self_test import _attempt_create

    sh = shutil.which("sh")
    if sh is None:
        print("FATAL: no 'sh' on PATH — cannot construct the ld-linux write-axis argv.")
        sys.exit(2)

    granted = Path(tempfile.mkdtemp(prefix="reyn-sandbox-loadwitness-w-")).resolve()
    denied = Path(tempfile.mkdtemp(prefix="reyn-sandbox-loadwitness-w-deny-")).resolve()
    try:
        policy = SandboxPolicy(
            write_paths=[str(granted)], network=False, deny_subprocess=False,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )
        # ld-linux directly on argv[0], /bin/sh as its target — the loader
        # dispatch, not the kernel's own PT_INTERP resolution of a bare `sh`.
        control = granted / "control"
        argv = [ld_linux, sh, "-c", f"echo x > {control}"]
        created, detail = _attempt_create(backend, policy, control, argv)
        _record(
            "[ld-linux][write] positive control: a write INSIDE the grant succeeds",
            created, detail,
        )
        if not created:
            _record(
                "[ld-linux][write] deny", False,
                "skipped — the positive control failed, so a denied write would prove nothing",
            )
            return

        escape = denied / "escape"
        argv = [ld_linux, sh, "-c", f"echo x > {escape}"]
        created, detail = _attempt_create(backend, policy, escape, argv)
        _record(
            "[ld-linux][write] a write OUTSIDE the grant is DENIED",
            not created, detail,
        )
    finally:
        shutil.rmtree(granted, ignore_errors=True)
        shutil.rmtree(denied, ignore_errors=True)


def _network_axis_ld_linux(ld_linux: str, backend) -> None:
    from reyn.security.sandbox.policy import SandboxPolicy
    from reyn.security.sandbox.self_test import _attempt_create

    granted = Path(tempfile.mkdtemp(prefix="reyn-sandbox-loadwitness-n-")).resolve()
    listener = _socket_mod.socket(_socket_mod.AF_INET, _socket_mod.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]

        def _connect_code(marker: Path) -> str:
            return (
                "import socket\n"
                "c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                f"c.connect(('127.0.0.1', {port}))\n"
                f"open({str(marker)!r}, 'w').close()\n"
            )

        policy_on = SandboxPolicy(
            write_paths=[str(granted)], network=True, deny_subprocess=False,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )
        policy_off = SandboxPolicy(
            write_paths=[str(granted)], network=False, deny_subprocess=False,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )

        control = granted / "control-connect"
        argv = [ld_linux, sys.executable, "-c", _connect_code(control)]
        created, detail = _attempt_create(backend, policy_on, control, argv)
        _record(
            "[ld-linux][network] positive control: connect() under network=True succeeds",
            created, detail,
        )
        if not created:
            _record(
                "[ld-linux][network] deny", False,
                "skipped — the positive control failed, so a denied connect() would prove nothing",
            )
            return

        escape = granted / "escape-connect"
        argv = [ld_linux, sys.executable, "-c", _connect_code(escape)]
        created, detail = _attempt_create(backend, policy_off, escape, argv)
        _record(
            "[ld-linux][network] connect() under network=False is DENIED",
            not created, detail,
        )
    finally:
        listener.close()
        shutil.rmtree(granted, ignore_errors=True)


def _write_axis_mmap(backend) -> None:
    from reyn.security.sandbox.policy import SandboxPolicy
    from reyn.security.sandbox.self_test import _attempt_create

    granted = Path(tempfile.mkdtemp(prefix="reyn-sandbox-loadwitness-mw-")).resolve()
    denied = Path(tempfile.mkdtemp(prefix="reyn-sandbox-loadwitness-mw-deny-")).resolve()
    try:
        policy = SandboxPolicy(
            write_paths=[str(granted)], network=False, deny_subprocess=False,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )
        control = granted / "control"
        argv = [sys.executable, "-c", _MMAP_OPEN_CHILD_SOURCE.format(path=str(control))]
        created, detail = _attempt_create(backend, policy, control, argv)
        _record(
            "[mmap-load][write] positive control: an mmap-loaded open() INSIDE the grant succeeds",
            created, detail,
        )
        if not created:
            _record(
                "[mmap-load][write] deny", False,
                "skipped — the positive control failed, so a denied open() would prove nothing",
            )
            return

        escape = denied / "escape"
        argv = [sys.executable, "-c", _MMAP_OPEN_CHILD_SOURCE.format(path=str(escape))]
        created, detail = _attempt_create(backend, policy, escape, argv)
        _record(
            "[mmap-load][write] an mmap-loaded open() OUTSIDE the grant is DENIED",
            not created, detail,
        )
    finally:
        shutil.rmtree(granted, ignore_errors=True)
        shutil.rmtree(denied, ignore_errors=True)


def _network_axis_mmap(backend) -> None:
    from reyn.security.sandbox.policy import SandboxPolicy
    from reyn.security.sandbox.self_test import _attempt_create

    granted = Path(tempfile.mkdtemp(prefix="reyn-sandbox-loadwitness-mn-")).resolve()
    listener = _socket_mod.socket(_socket_mod.AF_INET, _socket_mod.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]

        policy_on = SandboxPolicy(
            write_paths=[str(granted)], network=True, deny_subprocess=False,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )
        policy_off = SandboxPolicy(
            write_paths=[str(granted)], network=False, deny_subprocess=False,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )

        control = granted / "control-connect"
        argv = [
            sys.executable, "-c",
            _MMAP_CONNECT_CHILD_SOURCE.format(port=port, marker=str(control)),
        ]
        created, detail = _attempt_create(backend, policy_on, control, argv)
        _record(
            "[mmap-load][network] positive control: an mmap-loaded connect() "
            "under network=True succeeds",
            created, detail,
        )
        if not created:
            _record(
                "[mmap-load][network] deny", False,
                "skipped — the positive control failed, so a denied connect() would prove nothing",
            )
            return

        escape = granted / "escape-connect"
        argv = [
            sys.executable, "-c",
            _MMAP_CONNECT_CHILD_SOURCE.format(port=port, marker=str(escape)),
        ]
        created, detail = _attempt_create(backend, policy_off, escape, argv)
        _record(
            "[mmap-load][network] an mmap-loaded connect() under network=False is DENIED",
            not created, detail,
        )
    finally:
        listener.close()
        shutil.rmtree(granted, ignore_errors=True)


def main() -> int:
    ld_linux = _preflight()
    print(f"\\nld-linux interpreter = {ld_linux}")

    from reyn.security.sandbox.backends.landlock import LandlockBackend

    backend = LandlockBackend()

    print("\\n=== load method (a): ld-linux direct invocation ===")
    _write_axis_ld_linux(ld_linux, backend)
    _network_axis_ld_linux(ld_linux, backend)

    print("\\n=== load method (b): mmap(PROT_EXEC) + raw function-pointer call, no exec*() ===")
    _write_axis_mmap(backend)
    _network_axis_mmap(backend)

    print("\\n=== Summary ===")
    failed = [c for c in CHECKS if not c.ok]
    for c in CHECKS:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.label}")
    if not CHECKS:
        print("FATAL: zero checks recorded — this gate observed nothing and must not report green.")
        return 2
    if failed:
        print(
            f"\\n{len(failed)}/{len(CHECKS)} check(s) FAILED — the sandbox boundary is NOT "
            "load-method-independent on this host. This is the true-hole case #3229's issue "
            "body names as a P0, separate from and more urgent than #3227."
        )
        return 1
    print(f"\\nAll {len(CHECKS)} checks passed — the boundary held across both load methods.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
