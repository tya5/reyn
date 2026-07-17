"""Network-namespace isolation — the actual `policy.network=False` boundary on
Linux (#3030).

**What #3030 measured.** `network: false` was silently unenforced whenever
`allow_subprocess: true` (the stdio-MCP default): `landlock_exec._apply_seccomp`
/ `landlock.py`'s `_child_preexec` gate the WHOLE seccomp-BPF filter on
`allow_subprocess`, but the network deny lives INSIDE that same filter
(`_NETWORK_SYSCALLS` in `seccomp.py`, allowlisted only when `policy.network`).
Skip the filter and the network deny goes with it. Measured on Linux 6.8
(colima, Ubuntu 24.04, aarch64): a real outbound `connect()`+`send()` SUCCEEDED
under `network=False, allow_subprocess=True`.

**Why the fix is not "enumerate the network syscalls in a denylist".** A
denylist can only refuse syscalls it NAMES. `_NETWORK_SYSCALLS` has no
`io_uring_setup`/`io_uring_enter` entries — and `IORING_OP_CONNECT` /
`IORING_OP_SEND` reach the network without ever calling `connect()`/`sendto()`
as a syscall, so a denylist mirroring that set is walked around by io_uring
without calling a single named syscall. Enumerating syscalls is a moving
target (32-bit `socketcall`, a future io_uring opcode, a write to an inherited
socket fd); it is not bounded by construction, only by how complete today's
list happens to be.

**The fix: put the process in an interface-less network namespace.**
`unshare(CLONE_NEWNET)` gives the calling process a namespace with no network
interfaces (not even loopback is configured) and an empty routing table. Every
socket API — `connect()`, `sendto()`, an inherited fd's `write()`, io_uring's
`IORING_OP_CONNECT`/`IORING_OP_SEND` — bottoms out in the same in-kernel
`sock->ops->connect()` / route-lookup code, which now has nowhere to route a
packet to. This is a namespace boundary, not a syscall-name boundary: nothing
needs enumerating, because there is no interface to reach regardless of which
syscall (or io_uring opcode) asks. Same posture macOS Seatbelt already has via
SBPL's `(deny network*)` at the kernel-object level; this closes the
Linux-specific gap, it does not change macOS.

**Unprivileged namespace creation needs `CLONE_NEWUSER` too.** A non-root
process cannot `unshare(CLONE_NEWNET)` alone (requires `CAP_NET_ADMIN` in the
CURRENT user namespace); entering a fresh user namespace FIRST grants full
capabilities WITHIN that new namespace, which is what makes the following
`CLONE_NEWNET` permitted without root. This is the same `unshare
--user --net`-family pattern rootless container tools use. Both flags are
requested in ONE `unshare()` call so the process never has a fresh user
namespace without a fresh net namespace.

**Identity (uid/gid) mapping is best-effort, not the security property.**
Without writing `/proc/self/{uid,gid}_map`, the process's OWN view of its uid
(`os.getuid()`) becomes the kernel's overflow id (`65534`/"nobody") inside the
new user namespace — cosmetic, because DAC permission checks compare the
process's REAL kernel uid (fixed at `unshare()` time), not the namespace-local
view a later `getuid()` reports. Measured (colima, Ubuntu 24.04, kernel 6.8,
`/proc/sys/kernel/apparmor_restrict_unprivileged_userns=1`): `unshare()` itself
SUCCEEDS, but the subsequent `setgroups`/`uid_map`/`gid_map` writes are refused
with `EPERM` by AppArmor's unprivileged-userns mediation regardless — yet a
file this process owns was still written, read back, and reported its correct
host-side owner afterward. So a mapping failure does not touch the property
this module exists for (network reachability); it is logged and swallowed, not
raised. A caller who needs `os.getuid()`/`getpwuid()` to report the real
identity inside the sandboxed process does not get that on such a host — a
documented residual, matching the `read_deny_paths` asymmetry already
disclosed in `backends/landlock.py`.

**Fail-closed, not fail-open.** If the `unshare()` call itself fails (e.g.
`/proc/sys/kernel/unprivileged_userns_clone=0`, or a per-namespace-count limit
exhausted), :func:`isolate_network_namespace` RAISES — callers must refuse to
run the target rather than let it start with network reachable. A host that
cannot deliver the isolation this function exists for must not run the target
believing it delivered it.
"""
from __future__ import annotations

import ctypes
import logging
import os
import platform

_logger = logging.getLogger(__name__)

# linux/sched.h clone flags — stable ABI constants, not probed at runtime.
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000


def _unshare(flags: int) -> None:
    """Thin ctypes wrapper over libc's `unshare(2)`. Raises OSError with a real
    errno on failure.

    Deliberately `ctypes.CDLL(None, ...)` (the already-loaded symbols of the
    running process), NOT `ctypes.util.find_library` — the latter shells out
    and writes a temp file to locate the library (`seccomp.py`'s
    `preload_native_dependency` docstring measures this for `pyseccomp`, #3020)
    and libc is already mapped into any running CPython process, so there is
    nothing to locate.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    rc = libc.unshare(flags)
    if rc != 0:
        errno_num = ctypes.get_errno()
        raise OSError(errno_num, os.strerror(errno_num))


def isolate_network_namespace() -> None:
    """Move the CURRENT process into a fresh, interface-less network namespace.

    Call this in a re-exec shim before the target `exec`s (namespaces are
    per-process and are inherited across `fork`/`exec`, so applying it once
    here restricts everything downstream), or from a `preexec_fn` in the
    fork-then-exec `subprocess.Popen` path — before Landlock/seccomp are
    applied, since neither of those layers governs namespace syscalls and
    doing it first means the identity-map file writes below (best-effort) run
    while the process can still reach `/proc/self/*`.

    Raises:
        RuntimeError: the namespace could not be created (non-Linux, or the
            `unshare()` syscall itself failed) — the caller MUST refuse to run
            the target unrestricted rather than treat this as best-effort.
    """
    if platform.system() != "Linux":
        raise RuntimeError(
            "network-namespace isolation is Linux-only "
            f"(unshare(CLONE_NEWNET) has no equivalent on {platform.system()})"
        )

    # Captured BEFORE unshare(): once inside the fresh user namespace and
    # before any uid_map is written, os.getuid()/os.getgid() report the
    # kernel's overflow id, not the real identity — mapping that back to
    # itself would be a no-op that defeats the point of mapping at all.
    real_uid = os.getuid()
    real_gid = os.getgid()

    try:
        _unshare(_CLONE_NEWUSER | _CLONE_NEWNET)
    except OSError as exc:
        raise RuntimeError(
            "unshare(CLONE_NEWUSER|CLONE_NEWNET) failed "
            f"(errno={exc.errno}: {exc.strerror}) — this host cannot isolate "
            "the sandboxed process into a network-less namespace. Common "
            "causes: unprivileged user namespaces disabled "
            "(/proc/sys/kernel/unprivileged_userns_clone=0) or a per-user/"
            "per-container namespace limit exhausted. Refusing to run with "
            "policy.network=False rather than run it with network reachable "
            "(#3030)."
        ) from exc

    # Best-effort identity preservation — see module docstring. A failure here
    # is NOT fail-closed material: the network property this function exists
    # for is already in effect by the time we reach these lines (unshare()
    # above already succeeded), so we log and continue rather than raise.
    #
    # The map is `real_uid -> real_uid` (a TRUE 1:1 identity map), not
    # `0 -> real_uid` — the latter is the "fake root" idiom `unshare
    # --map-root-user` uses deliberately (make the caller APPEAR as uid 0
    # inside the namespace), which is the opposite of what "preserve identity"
    # means here: a sandboxed target must see the SAME `os.getuid()` it would
    # have outside the sandbox, not an artificially elevated one a target that
    # branches on `getuid() == 0` could react to differently.
    try:
        with open("/proc/self/setgroups", "w") as fh:
            fh.write("deny")
        with open("/proc/self/uid_map", "w") as fh:
            fh.write(f"{real_uid} {real_uid} 1")
        with open("/proc/self/gid_map", "w") as fh:
            fh.write(f"{real_gid} {real_gid} 1")
    except OSError as exc:
        _logger.debug(
            "netns: uid/gid identity map not applied (%s) — network isolation "
            "still holds; os.getuid()/os.getgid() will report the namespace "
            "overflow id (65534) rather than the real identity in the "
            "sandboxed process",
            exc,
        )


# Process-global cache — the probe below forks a throwaway child to observe a
# real result, and that result is a property of the HOST (kernel config,
# AppArmor policy, namespace-count limits), not of anything per-call. Mirrors
# `seccomp.is_available()`'s module-level cache for the same reason.
_AVAILABLE: bool | None = None


def netns_available() -> bool:
    """Return True iff :func:`isolate_network_namespace` actually SUCCEEDS on
    this host, right now.

    A real probe, not a capability guess: it forks a short-lived child that
    attempts the real isolation and reports its outcome via exit code, then
    waits for it. This is deliberately NOT "is Linux and is the syscall
    resolvable" — #2980 and #2962 are both examples in this codebase of a
    presence check reporting healthy while the actual mechanism was dead, and
    the point of a namespace probe is to not repeat that here for netns.

    The child does no cleanup beyond ``os._exit`` — matching `_child_preexec`'s
    own posture that a `fork()`-adjacent child must not run Python-level
    teardown that could deadlock on inherited locks.

    Cached after the first call (module-global, like `seccomp.is_available()`)
    since the host condition this probes does not change within a process's
    lifetime in any way callers can act on.
    """
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE

    if platform.system() != "Linux":
        _AVAILABLE = False
        return False

    pid = os.fork()
    if pid == 0:
        # Child: never returns to the caller. Any exception (including a
        # RuntimeError from isolate_network_namespace itself) means "not
        # available" here.
        try:
            isolate_network_namespace()
            os._exit(0)
        except BaseException:  # noqa: BLE001 — this process never returns either way
            os._exit(1)

    _, status = os.waitpid(pid, 0)
    _AVAILABLE = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    return _AVAILABLE


def _reset_for_tests() -> None:
    """Test hook: clear the availability cache."""
    global _AVAILABLE
    _AVAILABLE = None
