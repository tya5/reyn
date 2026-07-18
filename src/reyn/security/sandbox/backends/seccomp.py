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

Unconditional load (#3030). Both production callsites (`landlock.py`'s
`_child_preexec`, `landlock_exec.py`'s `_apply_seccomp`) used to skip this
filter ENTIRELY whenever `policy.allow_subprocess` was True — the stdio-MCP
default — which silently dropped the NETWORK gate (`_NETWORK_SYSCALLS` are
allowlisted only when `policy.network`) along with the syscall-reduction one.
Both callsites now load this filter unconditionally; `_build_syscall_allowlist`
already adds `_SUBPROCESS_SYSCALLS`/`_NETWORK_SYSCALLS` per-policy, so this was
a caller-side gate, not a builder change. The practical consequence: every
`allow_subprocess: True` MCP server (the default) is now under this default-deny
allowlist for the first time, which is exactly the #2962 correctness risk this
module's validation history above is about — see
`tests/test_sandbox_seccomp_network_3030.py` for the representative-real-MCP-server
probes (`reyn-rag-chunker` / `reyn-rag-vector-store` / `uvx markitdown-mcp`) this
change specifically needed, on top of the synthetic echo/ls/cat workloads above.
That risk materialized in review (#3059), across two x86_64 CI rounds the aarch64
completeness witness had not surfaced:
  - round 1: `_BASELINE` lacked the async-runtime event-loop startup primitives
    (`socketpair` for CPython's asyncio self-pipe, `eventfd` for Tokio's mio
    waker) EVERY stdio MCP server needs — see the "Async-runtime event-loop
    startup primitives" block.
  - round 2: it lacked the durability/locking-on-open-fd primitives (`fsync`/
    `fdatasync` — SQLite "disk I/O error" in reyn-rag-vector-store; `flock` — uv's
    cache lock in `uvx markitdown-mcp`) — see the "Durability + advisory
    file-locking" block.
Both are witnessed by the x86_64 deny-gate job, not an aarch64 host whose green
does not speak for the enforce arch. The representative-server completeness
probes run at `network=True` (the server's own network needs met, so the only
variable vs baseline is the syscall filter): a FastMCP/uvx server issues
network-family syscalls during init, which `network=False` now CORRECTLY denies
(that is #3030's fix, not a gap), so a `network=False` server run witnesses the
network gate, not allowlist completeness — the latter is a `network=True`
question, the former is covered precisely by the dedicated socket/io_uring deny
probes.

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
    # Async-runtime event-loop startup primitives (#3059, the #3030 fix's #2962
    # blast radius made concrete). When #3030 made the filter load
    # unconditionally, every stdio MCP server — all built on an async runtime —
    # came under this default-deny allowlist for the FIRST time, and these were
    # absent, so the runtime could not even start. Primary data (deny-gate job,
    # x86_64/ubuntu-24.04, measured — NOT the aarch64 host the completeness
    # witness first ran on, which is exactly why it missed these):
    #   - `_socket.socketpair()` -> EPERM: CPython's asyncio event loop builds
    #     its wakeup self-pipe (`_ssock`/`_csock`) with socketpair() at startup,
    #     so FastMCP / anyio / asyncio servers (reyn-rag-chunker,
    #     reyn-rag-vector-store) died in `initialize()` before serving anything.
    #   - Tokio (uvx / Rust): "Failed building the Runtime: PermissionDenied" —
    #     mio's I/O-driver waker uses eventfd on Linux, so `uvx markitdown-mcp`
    #     could not construct its runtime.
    #
    # None of these can reach the network or escape the sandbox — each is a
    # LOCAL-fd/IPC primitive, so adding them does NOT reopen #3030 (the network
    # gate is `_NETWORK_SYSCALLS`, still gated on `policy.network`):
    #   - socketpair: a connected pair in ONE address family, local only. On
    #     Linux only AF_UNIX is supported (AF_INET/AF_INET6 -> EOPNOTSUPP), so it
    #     cannot create a network socket — `socket`/`connect` stay network-gated.
    #   - eventfd/eventfd2: a counter object fd for wakeups; no I/O target but
    #     itself. (glibc's eventfd() issues the eventfd2 syscall; both named so
    #     libseccomp resolves whichever the running libc uses.)
    #   - timerfd_create/settime/gettime: a timer-expiration fd; delivers time,
    #     not data. (Node/libuv async timers.)
    #   - signalfd4: delivers pending signals as fd reads; no network.
    #   - epoll_create: readiness monitor over fds the process already holds
    #     (epoll_create1 is already baseline above; the legacy create is added
    #     for runtimes/libc that still issue it).
    # Derived as a CLASS, not one syscall at a time: fixing socketpair alone
    # would only surface eventfd as the next deny (#3059 co-vet). Witnessed on
    # x86_64 (the enforce arch) by the representative-server group in
    # tests/test_sandbox_seccomp_network_3030.py + the deny-gate CI job, not on
    # the aarch64 host whose green did not speak for x86_64.
    "socketpair",
    "eventfd", "eventfd2",
    "timerfd_create", "timerfd_settime", "timerfd_gettime",
    "signalfd4",
    "epoll_create",
    # Durability + advisory file-locking on ALREADY-OPEN fds (#3059, 2nd x86_64
    # CI round). Completing the "I/O on already-opened fds" category the baseline
    # opened at the top (read/write/lseek/close/fcntl/fstat) — these operate on an
    # fd Landlock already adjudicated at open, so allowing them grants no new path
    # or network access. Primary data (deny-gate job, x86_64):
    #   - reyn-rag-vector-store's `list_metadata` returned SQLite "disk I/O error"
    #     (SQLITE_IOERR) creating a fresh db — SQLite's unix VFS fsync/fdatasync
    #     on the first commit was refused (fcntl locking is already baseline, so
    #     the remaining durability syscall is the gap). Network-INDEPENDENT: it
    #     failed at network=True too, which is what distinguishes it from the
    #     FastMCP-startup-network failures below.
    #   - `uvx markitdown-mcp` (uv, Rust): "failed to lock
    #     `~/.cache/uv/.lock`: Operation not permitted" — uv `flock`s its cache.
    # Each is an fd/durability primitive with no network reach:
    #   - fsync/fdatasync: flush an open fd's data to disk.
    #   - sync_file_range: flush a byte-range of an open fd (glibc/kernel may
    #     route a partial sync through it) — same class, added so the durability
    #     gap is closed rather than re-surfacing as the next RED.
    #   - flock: a BSD advisory lock on an already-open fd (no path, no network).
    "fsync", "fdatasync", "sync_file_range",
    "flock",
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


def preload_native_dependency() -> None:
    """Resolve pyseccomp's native libraries NOW, while the calling process can
    still reach the filesystem — the ordering :func:`load_seccomp_filter` depends
    on and cannot enforce itself (#3020).

    ``pyseccomp``'s module body is not inert: at import it runs
    ``ctypes.CDLL(ctypes.util.find_library("c"))`` and ``find_library("seccomp")``,
    and ``find_library`` writes a temp file and shells out to ``gcc``/``ldconfig``
    to do it. Landlock denies all three once a ruleset is applied, so an import
    deferred until after ``apply()`` dies — measured on Linux 6.8 as
    ``FileNotFoundError: No usable temporary directory found``, and as ``Unable to
    find libseccomp`` even when ``/tmp`` is granted. The filter then never loads,
    which is #2962's outcome reached by a second route.

    Call this BEFORE the Landlock restriction goes on: in the shim, before
    ``ruleset.apply()``; on the ``run()`` seam, in the PARENT before the fork,
    since ``preexec_fn`` executes in an already-restricted child. Afterwards the
    module is in ``sys.modules`` (and inherited across ``fork``), so
    ``load_seccomp_filter`` only USES an already-resolved library and touches no
    path.

    It is a separate named call rather than an ``is_available()`` invoked for its
    side effect: the ordering is the security property here, and a bare
    availability check at that callsite reads like a redundant probe that a later
    reader would move or drop. Safe to call unconditionally — non-Linux or
    pyseccomp-missing makes it a no-op, exactly as ``is_available()`` is.
    """
    is_available()


def load_seccomp_filter(policy: SandboxPolicy) -> None:
    """Load a default-deny seccomp-BPF filter into the CURRENT process.

    Call this from the child side of a fork: from (or composed into) a
    `preexec_fn` for `subprocess.Popen`, or immediately before an exec in a
    re-exec shim. It applies to the calling thread at once, is irrevocable, and
    survives the `execve` that follows — which is why `execve` itself must be in
    the allowlist (`_BASELINE`).

    ⚠ It has a PRECONDITION it cannot enforce: `pyseccomp` must already be
    imported when this runs, because that import resolves native libraries via
    the filesystem and Landlock has, by this point, denied it. Callers satisfy it
    with `preload_native_dependency()` before the restriction goes on; see #3020
    for the measurement.

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
