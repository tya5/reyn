"""seccomp-BPF syscall filter — stacks on top of Landlock (FP-0017).

LandlockBackend handles filesystem and network rules at the kernel-object
level; seccomp-BPF complements it by reducing the syscall surface available
to the sandboxed process. They are orthogonal — Landlock cannot block
`ptrace`, but seccomp-BPF can.

The `pyseccomp` PyPI package wraps libseccomp. This module is import-guarded:
if `pyseccomp` is unavailable, `is_available()` returns False and callers
(LandlockBackend) lazily fall through.

Validation status (#2962). Until #2962 this filter had NEVER loaded in
production: the entry point returned an installer callable and both callsites
discarded it, so `f.load()` never ran and the allowlist below was never
exercised. The entry point is now a plain side-effecting `load_seccomp_filter`
with no return value, so a caller cannot silently drop the load. With the wiring
fixed, the allowlist is live and a name missing from it makes that syscall fail
with EPERM in the sandboxed process — so this list is a correctness surface, not
just a hardening surface.

- aarch64 Linux (Ubuntu 24.04, kernel 6.8, glibc 2.39): live-validated —
  the filter loads and ordinary workloads (echo / ls / cat / python file
  read+write, mkdir / remove / rename / rmtree) run under it, while fork,
  ptrace and socket are refused.
- x86_64 Linux: NOT validated. The maintainer dev environment is macOS/arm64
  and x86_64 is reachable there only under Rosetta emulation, where
  `seccomp_load()` fails with ECANCELED — an artifact of installing an
  x86_64 filter against an aarch64 kernel, which says nothing about real
  x86_64 hardware. Every allowlist name does resolve on x86_64 (libseccomp
  accepts each `add_rule`), but runtime sufficiency there is unproven.
  Contributors on x86_64 Linux are invited to confirm.
"""
from __future__ import annotations

import errno
import logging
import platform

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
    # Legacy stat entry points. NOT live-validated and NOT validatable on the
    # maintainer's aarch64 host, where these syscalls do not exist at all (arm64
    # has only the *at forms) — so an aarch64 run is structurally blind to their
    # absence. x86_64 glibc < 2.33 issues __NR_stat/__NR_lstat directly (glibc
    # 2.33 routed them through newfstatat), so on Debian 11 / Ubuntu 20.04 /
    # RHEL 8 / Amazon Linux 2 the first stat() would otherwise be refused.
    # Included on that basis; harmless where the syscall does not exist.
    "stat", "lstat",
    # Ordinary file management (live-validated, #2962).
    #
    # These were deliberately EXCLUDED on the theory that "write access is
    # governed by Landlock path rules, not seccomp". That reasoning does not
    # survive contact with a default-deny filter: a syscall absent from the
    # allowlist is not delegated to Landlock, it is refused here before Landlock
    # can adjudicate anything. Measured: os.mkdir / os.remove / os.rename /
    # shutil.rmtree were all killed outright by the filter.
    #
    # ⚠ The "Landlock adjudicates it" argument is only valid for a right in the
    # HANDLED set that LandlockBackend builds (landlock.py: read_rules |
    # write_rules — MAKE_DIR / MAKE_REG / MAKE_SYM / REMOVE_FILE / REMOVE_DIR /
    # REFER / WRITE_FILE). Every name below is covered by that set, so allowing
    # it here grants no path access: Landlock still denies outside write_paths.
    # A syscall Landlock CANNOT govern (chmod, chown) or that is not in the
    # handled set (truncate) must NOT be added on this rationale — nothing would
    # then stop it. See `_EXCLUDED_UNGOVERNABLE` below.
    #
    # Measured on aarch64: getcwd, mkdirat, unlinkat, renameat, symlinkat. The
    # legacy non-*at aliases are included alongside for x86_64 (where glibc may
    # route to them); libseccomp tolerates names absent on the running arch,
    # which is why the pre-existing list can carry both `open` and `openat`.
    "getcwd",
    "mkdir", "mkdirat", "rmdir",
    "unlink", "unlinkat",
    "rename", "renameat", "renameat2",
    "symlink", "symlinkat", "link", "linkat",
    # ftruncate only: it acts on an already-open fd, so Landlock adjudicates it
    # indirectly via WRITE_FILE on the open. Path-based truncate(2) is excluded.
    "ftruncate",
    # Pipes (live-validated, #2962 co-vet). These create fds, not processes: no
    # path access, no process creation, and Docker's default profile allows them.
    #
    # They are what makes the #2820 launcher-fork mitigation work on Linux. A
    # shell that cannot pipe fails BEFORE it reaches fork(), printing "pipe
    # error: Operation not permitted" / "cannot make pipe for command
    # substitution" — messages denial.py cannot classify — instead of the
    # "fork: Operation not permitted" it matches on. Measured across four shell
    # launcher shapes, classification went 1/4 -> 3/4 by allowing these. That
    # also restores parity with macOS Seatbelt, whose `(deny process-fork)`
    # blocks fork while leaving pipe() alone.
    #
    # dup/dup2/dup3 were measured alongside and changed nothing, so they are not
    # added. Remaining known gap: dash prints "Cannot fork", which denial.py's
    # regex does not match — a pre-existing limit of that regex, not of this
    # filter (dash's fork is correctly refused either way).
    "pipe", "pipe2",
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

# Deliberately NOT in _BASELINE, recorded so the "Landlock adjudicates it"
# rationale cannot be used to re-add them (#2962 co-vet).
#
# The rationale holds only for rights in LandlockBackend's HANDLED set. For
# these, no layer would stop the call:
#
#   chmod / fchmod / fchmodat  Landlock has NO chmod right at all. The kernel
#                              docs list it under current limitations: "It is
#                              currently not possible to restrict some
#                              file-related actions … chmod(2), chown(2)". This
#                              is the same reason `chown` was already excluded —
#                              allowing chmod while excluding chown would have
#                              been internally inconsistent.
#   truncate                   FSAccess.TRUNCATE exists (ABI 3+) but is NOT in
#                              the handled set, so Landlock does not govern it.
#                              Granting it in the handled set was considered and
#                              rejected: the grant is ABI-gated, so on ABI 1–2
#                              hosts (kernel 5.13–6.1) truncate would still be
#                              ungoverned while seccomp allowed it — a hole that
#                              appears only on older kernels. Excluding it here
#                              is uniformly correct across every ABI.
#
# Falsification that motivated this (with policy.write_paths=[workspace]):
#   os.truncate("~/.ssh/id_ed25519", 0)  and  os.chmod("~/.ssh", 0o777)
# would pass seccomp, be invisible to Landlock, and succeed under DAC (same uid)
# — destructive writes outside write_paths that no layer stops. Not a regression
# versus today (the filter never loaded), but it must not be claimed as blocked.
#
# No validated workload needs them: none of the nine live-verified workloads
# (echo / ls / cat / python print / read+write / mkdir / remove / rename /
# rmtree) issues chmod or path-based truncate. They entered an earlier revision
# only because a synthetic probe exercised them — an unforced widening.
_EXCLUDED_UNGOVERNABLE: list[str] = [
    "chmod", "fchmod", "fchmodat", "chown", "fchown", "lchown", "fchownat",
    "truncate",
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


def load_seccomp_filter(policy: SandboxPolicy) -> None:
    """Load a default-deny seccomp-BPF filter into the CURRENT process.

    Call this from the child side of a fork: from (or composed into) a
    `preexec_fn` for `subprocess.Popen`, or immediately before an exec in a
    re-exec shim. It applies to the calling thread at once, is irrevocable, and
    survives the `execve` that follows — which is why `execve` itself must be in
    the allowlist (`_BASELINE`).

    There is deliberately NO deferred form and NO return value. This function
    used to be `install_seccomp_filter`, which *returned* an installer that the
    caller had to invoke; both production callsites called it and dropped the
    result, so the filter never loaded and the layer was dead for its entire
    existence (#2962). A builder makes "call it and discard" a silent no-op that
    reads, at the callsite, exactly like working code. With a plain side-effecting
    call that defect is not constructable: the only way to misuse this is to not
    call it at all, which a reader can see. Prefer the structure that cannot
    express the bug over a name that merely describes it accurately.

    If seccomp is unavailable (non-Linux or pyseccomp not installed) this is a
    no-op that emits a WARN — Landlock alone still applies, so isolation is not
    entirely absent.

    ⚠ Every syscall NOT in the allowlist is refused with EPERM. The allowlist is
    a correctness surface, not just a hardening surface; see the module docstring
    for what is live-validated and what is not, and `_EXCLUDED_UNGOVERNABLE` for
    names that must not be added on a "Landlock will adjudicate it" rationale.

    Args:
        policy: The declarative sandbox policy governing which capabilities
            are permitted. `policy.network` and `policy.allow_subprocess`
            control which optional syscall groups are added to the allowlist.
    """
    if not is_available():
        _logger.warning(
            "seccomp-BPF unavailable (non-Linux or pyseccomp missing); "
            "skipping syscall filter — Landlock rules still apply"
        )
        return

    import pyseccomp  # noqa: PLC0415 — guarded by is_available()

    # Default-deny posture: any syscall not in the allowlist is REFUSED with
    # EPERM. Refusal, not death — and deliberately not `pyseccomp.KILL`, for
    # three reasons (#2962 co-vet):
    #
    #  1. `pyseccomp.KILL` is KILL_THREAD (0x0), not KILL_PROCESS (0x80000000).
    #     It kills the calling THREAD. For a single-threaded child that is
    #     equivalent to death, but under any change that lets threads exist it
    #     degrades to individual threads dying silently — a partial failure that
    #     presents as a hang or a wrong answer, not as an error.
    #  2. Killing produces no errno and no output, which silently disables the
    #     #2820 launcher-fork mitigation (`denial.py`) on Linux — a module that
    #     explicitly claims to cover "Linux seccomp" and detects denials by
    #     matching "fork: operation not permitted" on stderr. With EPERM the
    #     launcher prints that string and `classify_denial` fires as designed,
    #     giving true parity with Seatbelt's `(deny process-fork)`, which is
    #     also EPERM.
    #  3. It bounds the blast radius of an allowlist gap: a syscall we failed to
    #     validate (see the x86_64 gap in the module docstring) fails one call
    #     with EPERM, which callers routinely handle, instead of destroying the
    #     process. This also matches Docker's default (SCMP_ACT_ERRNO).
    #
    # Security is unchanged: EPERM refuses the syscall just as KILL does.
    f = pyseccomp.SyscallFilter(defaction=pyseccomp.ERRNO(errno.EPERM))

    for syscall_name in _build_syscall_allowlist(policy):
        f.add_rule(pyseccomp.ALLOW, syscall_name)

    # Irrevocable — issues prctl(PR_SET_SECCOMP) in the child process.
    f.load()


def _build_syscall_allowlist(policy: SandboxPolicy) -> list[str]:
    """Return the list of syscall names to permit under the given policy.

    Always includes the baseline minimum for a Linux process. Conditionally
    adds network and subprocess syscalls based on policy flags.

    Filesystem-mutating syscalls (unlink, mkdir, rename, …) ARE included, but
    only those whose Landlock right is in the HANDLED set LandlockBackend builds
    (`read_rules | write_rules`). For those, write access really is governed by
    Landlock path rules rather than seccomp, and the syscall must be allowed HERE
    to reach Landlock and be adjudicated at all — so allowing it grants no path
    access, while omitting it merely refused the call instead of delegating the
    decision (#2962).

    That rationale does NOT extend to syscalls Landlock cannot govern (chmod,
    chown) or that are absent from the handled set (path-based truncate): for
    those, nothing downstream would stop the call. They are listed in
    `_EXCLUDED_UNGOVERNABLE` with the reasoning.

    Escape-hatch syscalls (ptrace, process_vm_readv, keyctl, modify_ldt,
    request_key, add_key) are never included regardless of policy — refusal is
    the intended outcome. Process CREATION (fork/clone/…) is gated on
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
