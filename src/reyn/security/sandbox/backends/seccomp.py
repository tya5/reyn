"""seccomp-BPF syscall filter builder — stacks on top of Landlock (FP-0017).

LandlockBackend handles filesystem and network rules at the kernel-object
level; seccomp-BPF complements it by reducing the syscall surface available
to the sandboxed process. They are orthogonal — Landlock cannot block
`ptrace`, but seccomp-BPF can.

The `pyseccomp` PyPI package wraps libseccomp. This module is import-guarded:
if `pyseccomp` is unavailable, `is_available()` returns False and callers
(LandlockBackend) lazily fall through.

Validation status (#2962). Until #2962 this filter had NEVER loaded in
production: both callsites called the builder and discarded the installer it
returns, so `f.load()` never ran and the allowlist below was never exercised.
With the wiring fixed, the allowlist is live and a name missing from it KILLS
the sandboxed process — so this list is a correctness surface, not just a
hardening surface.

- aarch64 Linux (Ubuntu 24.04, kernel 6.8, glibc 2.39): live-validated —
  the filter loads and ordinary workloads (echo / ls / cat / python file
  read+write) run under it.
- x86_64 Linux: NOT validated. The maintainer dev environment is macOS/arm64
  and x86_64 is reachable there only under Rosetta emulation, where
  `seccomp_load()` fails with ECANCELED — an artifact of installing an
  x86_64 filter against an aarch64 kernel, which says nothing about real
  x86_64 hardware. Every allowlist name does resolve on x86_64 (libseccomp
  accepts each `add_rule`), but runtime sufficiency there is unproven.
  Contributors on x86_64 Linux are invited to confirm.
"""
from __future__ import annotations

import logging
import platform
from typing import Callable

from ..policy import SandboxPolicy

_logger = logging.getLogger(__name__)

# Lazy availability cache — None means "not yet checked".
_AVAILABLE: bool | None = None

# Baseline syscalls always permitted — minimum for a Linux process to function.
#
# Linux-validated (#2962). This list was never exercised before #2962 because the
# filter never loaded in production; the first live run killed /bin/echo. The
# entries below marked "live-validated" were discovered empirically by loading the
# real filter and running real commands under strace until they stopped dying —
# NOT by reading Docker/Firejail defaults. See the #2962 PR body for the method,
# the environment, and the residual gaps.
_BASELINE: list[str] = [
    # Process lifecycle
    "exit", "exit_group", "rt_sigreturn",
    # Memory management
    "brk", "mmap", "munmap", "mprotect", "madvise", "mremap",
    # I/O on already-opened fds (file ops go through Landlock, not seccomp)
    "read", "write", "readv", "writev", "pread64", "pwrite64",
    "lseek", "close", "fcntl", "fstat", "newfstatat", "statx",
    # poll / epoll for async I/O
    "poll", "ppoll", "epoll_create1", "epoll_ctl", "epoll_wait", "epoll_pwait",
    # Time
    "clock_gettime", "clock_getres", "gettimeofday", "nanosleep", "clock_nanosleep",
    # Signal handling
    "rt_sigaction", "rt_sigprocmask", "sigaltstack",
    # Process info (read-only)
    "getpid", "gettid", "getuid", "geteuid", "getgid", "getegid",
    "getppid", "getpgrp", "getpgid", "getsid",
    # Threads (libc may use these)
    "futex", "set_robust_list", "get_robust_list", "set_tid_address",
    # Misc
    "uname", "getrandom", "prctl", "arch_prctl", "sysinfo",
    # ld.so dlopen path
    "openat", "open", "access", "faccessat", "faccessat2", "readlink", "readlinkat",
    # Process exit
    "wait4", "waitid",
    # glibc process startup (live-validated, #2962). Every dynamically-linked
    # binary calls these before reaching main(); without them the filter killed
    # /bin/echo. `rseq` (restartable sequences) is registered unconditionally by
    # glibc >= 2.35; `prlimit64` backs getrlimit during startup.
    "rseq", "prlimit64",
    # Ordinary file/dir I/O on already-opened fds (live-validated, #2962).
    # `getdents64` = directory listing (ls), `statfs` = filesystem stat,
    # `fadvise64` = readahead hint (cat), `ioctl` = isatty()/termios probing that
    # virtually every libc program performs on its stdio fds. None of these
    # create processes or open new paths — path access stays governed by Landlock.
    "getdents64", "statfs", "fadvise64", "ioctl",
    # Ordinary file management (live-validated, #2962).
    #
    # These were deliberately EXCLUDED on the theory that "write access is
    # governed by Landlock path rules, not seccomp". That reasoning does not
    # survive contact with `defaction=KILL`: absent from a default-deny allowlist
    # does not mean "delegated to Landlock", it means the process is KILLED with
    # SIGSYS before Landlock can adjudicate anything. Measured under the live
    # filter: os.mkdir / os.remove / os.rename / shutil.rmtree were all killed.
    # Allowing the syscall here does NOT grant path access — Landlock still
    # denies it (EPERM) outside policy.write_paths. Allowing it is what lets
    # Landlock be the layer that decides, which was the documented intent.
    #
    # Measured on aarch64: getcwd, mkdirat, unlinkat, renameat, fchmodat,
    # truncate, symlinkat. The legacy non-*at aliases are included alongside for
    # x86_64 (where glibc may route to them); libseccomp tolerates names absent
    # on the running arch, which is why the pre-existing list can carry both
    # `open` and `openat`.
    "getcwd",
    "mkdir", "mkdirat", "rmdir",
    "unlink", "unlinkat",
    "rename", "renameat", "renameat2",
    "chmod", "fchmod", "fchmodat",
    "symlink", "symlinkat", "link", "linkat",
    "truncate", "ftruncate",
    # CPython's own post-preexec_fn child code (live-validated, #2962).
    # LandlockBackend loads the filter from a preexec_fn, so the syscalls
    # _posixsubprocess.fork_exec makes AFTER preexec_fn returns are filtered too:
    # CPython >= 3.9 closes inherited fds with close_range(). Without it the
    # child died between preexec_fn and execve — a shape the landlock_exec shim
    # (plain os.execvp) does NOT exercise, which is why both callsites were
    # validated separately.
    "close_range",
    # Replacing THIS process's image. Baseline — not gated on allow_subprocess.
    # Both callsites load the filter in a pre-exec position (LandlockBackend from
    # a preexec_fn, immediately before Popen's execve; landlock_exec from
    # _apply_landlock, immediately before os.execvp). The filter survives execve,
    # so denying execve here would KILL the sandboxed target before it ever
    # starts — i.e. it would deny the sandbox its own reason to exist (#2962).
    # This is NOT a subprocess capability: execve replaces the calling process
    # and spawns nothing. Spawning requires fork/vfork/clone/clone3, which stay
    # gated on allow_subprocess below.
    "execve", "execveat",
]

# Syscalls added when policy.network is True.
_NETWORK_SYSCALLS: list[str] = [
    "socket", "connect", "accept", "accept4", "bind", "listen",
    "sendto", "recvfrom", "sendmsg", "recvmsg",
    "getsockname", "getpeername", "setsockopt", "getsockopt", "shutdown",
]

# Syscalls added when policy.allow_subprocess is True. These are the syscalls
# that CREATE a new process; execve/execveat are baseline (see _BASELINE) because
# they only replace the current image.
#
# Consequence worth knowing: glibc's fork()/posix_spawn() and pthread_create()
# all route through clone(), so with allow_subprocess=False a target that spawns
# THREADS is killed too, not just one that spawns processes. That is inherent to
# gating clone and is unchanged by #2962 — flagged, not silently decided.
_SUBPROCESS_SYSCALLS: list[str] = [
    "fork", "vfork", "clone", "clone3",
]


def is_available() -> bool:
    """Return True iff seccomp-BPF filtering is usable in this environment.

    Requirements:
    - Running on Linux (seccomp-BPF is a Linux kernel feature).
    - `pyseccomp` package is installed (wraps libseccomp).

    Result is cached after the first call to avoid repeated import attempts.
    """
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE

    if platform.system() != "Linux":
        _AVAILABLE = False
        return False

    try:
        import pyseccomp  # noqa: F401
        _AVAILABLE = True
    except ImportError:
        _AVAILABLE = False

    return _AVAILABLE


def build_seccomp_installer(policy: SandboxPolicy) -> Callable[[], None]:
    """Build (do NOT load) a seccomp-BPF filter installer for *policy*.

    ⚠ This function has NO side effect. Nothing is filtered until the RETURNED
    callable is invoked — `build_seccomp_installer(policy)` on its own is dead
    code. The previous name (`install_seccomp_filter`) read as if calling it
    installed the filter; both production callsites therefore discarded the
    return value and the filter never loaded in production (#2962). The name
    now describes what the function does: it BUILDS an installer.

    The returned callable is intended to be used as (or composed into) a
    `preexec_fn` argument for `subprocess.Popen`. It runs in the child
    process after fork() but before exec(), where it loads a default-deny
    syscall filter via libseccomp.

    If seccomp is unavailable (non-Linux or pyseccomp not installed), the
    returned callable is a no-op that emits a WARN log — Landlock alone
    still applies, so isolation is not entirely absent.

    Args:
        policy: The declarative sandbox policy governing which capabilities
            are permitted. `policy.network` and `policy.allow_subprocess`
            control which optional syscall groups are added to the allowlist.

    Returns:
        A zero-argument callable suitable for use in a preexec_fn chain.
    """
    def _install() -> None:
        if not is_available():
            _logger.warning(
                "seccomp-BPF unavailable (non-Linux or pyseccomp missing); "
                "skipping syscall filter — Landlock rules still apply"
            )
            return

        import pyseccomp  # noqa: PLC0415 — guarded by is_available()

        # Default-deny posture: any syscall not in the allowlist kills the process
        # with SIGSYS. Live-validated on aarch64; see the module docstring for the
        # x86_64 gap.
        f = pyseccomp.SyscallFilter(defaction=pyseccomp.KILL)

        for syscall_name in _build_syscall_allowlist(policy):
            f.add_rule(pyseccomp.ALLOW, syscall_name)

        # Irrevocable — issues prctl(PR_SET_SECCOMP) in the child process.
        f.load()

    return _install


def _build_syscall_allowlist(policy: SandboxPolicy) -> list[str]:
    """Return the list of syscall names to permit under the given policy.

    Always includes the baseline minimum for a Linux process. Conditionally
    adds network and subprocess syscalls based on policy flags.

    Filesystem-mutating syscalls (unlink, mkdir, rename, …) ARE included: write
    access is governed by Landlock path rules, not seccomp, and under
    `defaction=KILL` a syscall must be allowed HERE to reach Landlock and be
    adjudicated at all. Allowing them grants no path access; omitting them merely
    killed the process instead of delegating the decision (#2962).

    Escape-hatch syscalls (ptrace, process_vm_readv, keyctl, modify_ldt,
    request_key, add_key) are never included regardless of policy — for those,
    KILL is the intended outcome. Process CREATION (fork/clone/…) is gated on
    policy.allow_subprocess; see `_SUBPROCESS_SYSCALLS`.

    Args:
        policy: The sandbox policy to derive the allowlist from.

    Returns:
        A list of syscall name strings.
    """
    allowed: list[str] = list(_BASELINE)

    if policy.network:
        allowed.extend(_NETWORK_SYSCALLS)

    if policy.allow_subprocess:
        allowed.extend(_SUBPROCESS_SYSCALLS)

    return allowed


def _reset_for_tests() -> None:
    """Test hook: clear the availability cache."""
    global _AVAILABLE
    _AVAILABLE = None
