"""seccomp-BPF syscall filter builder — stacks on top of Landlock (FP-0017).

LandlockBackend handles filesystem and network rules at the kernel-object
level; seccomp-BPF complements it by reducing the syscall surface available
to the sandboxed process. They are orthogonal — Landlock cannot block
`ptrace`, but seccomp-BPF can.

The `pyseccomp` PyPI package wraps libseccomp. This module is import-guarded:
if `pyseccomp` is unavailable, `is_available()` returns False and callers
(LandlockBackend) lazily fall through.

Contributor-friendly track: the maintainer dev environment is macOS-only.
Linux contributors are invited to validate the syscall filter list.
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
# TODO(fp-0017-b): Linux validation needed — list derived from Docker/Firejail
# defaults but may need real-process tuning for libc startup / dynamic loader.
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
]

# Syscalls added when policy.network is True.
_NETWORK_SYSCALLS: list[str] = [
    "socket", "connect", "accept", "accept4", "bind", "listen",
    "sendto", "recvfrom", "sendmsg", "recvmsg",
    "getsockname", "getpeername", "setsockopt", "getsockopt", "shutdown",
]

# Syscalls added when policy.allow_subprocess is True.
_SUBPROCESS_SYSCALLS: list[str] = [
    "fork", "vfork", "clone", "clone3", "execve", "execveat",
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


def install_seccomp_filter(policy: SandboxPolicy) -> Callable[[], None]:
    """Return a callable that installs a seccomp-BPF filter when invoked.

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

        # Default-deny posture: any syscall not in the allowlist kills the process.
        # TODO(fp-0017-b): Linux validation needed — pyseccomp API is reasonably
        # stable but the filter list may need tweaks for real Linux processes
        # (libc startup, dynamic loader, Python runtime internals).
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

    Destructive filesystem syscalls (unlink, mkdir, rename, …) are intentionally
    absent — write access is governed by Landlock path rules, not seccomp.
    Escape-hatch syscalls (ptrace, process_vm_readv, keyctl, modify_ldt,
    request_key, add_key) are never included regardless of policy.

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
