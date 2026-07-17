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

**No uid/gid identity mapping — deliberately.** Without writing
`/proc/self/{uid,gid}_map`, the process's OWN view of its uid (`os.getuid()`)
becomes the kernel's overflow id (`65534`/"nobody") inside the new user
namespace. An earlier revision attempted a best-effort identity map here (real
uid/gid mapped 1:1, so `os.getuid()` would still report the caller's real
identity); that attempt is what turned a live x86_64 CI host (Landlock
present, unprivileged user namespaces enabled, no AppArmor mediation blocking
the map writes) into a reproducible regression — a plain `ls /` under
`_child_preexec` started failing with `Permission denied` on `opendir("/")`
specifically once the map writes started succeeding there, for a mechanism not
root-caused before this revision shipped. Network reachability (the actual
property this module exists for) is established by `unshare()` alone, before
any map write would happen, so dropping the map removes an unexplained
interaction without touching the security property. A caller who needs
`os.getuid()`/`getpwuid()` to report the real identity inside the sandboxed
process does not get that — a documented residual, matching the
`read_deny_paths` asymmetry already disclosed in `backends/landlock.py`.

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
    fork-then-exec `subprocess.Popen` path. It performs NO `/proc/self/*`
    identity-map writes (see module docstring for why the map was dropped), so
    it has no ordering dependency on filesystem reachability and may run before
    or after Landlock/seccomp — though it must stay BEFORE seccomp, since
    `unshare` is not in the seccomp allowlist and would be refused after the
    filter loads.

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

    # No uid/gid identity mapping. An earlier revision attempted a best-effort
    # `real_uid -> real_uid` map here (never required for the security property
    # — see module docstring) and that attempt is what a live x86_64/Landlock
    # CI host (kernel with unprivileged user namespaces enabled and no
    # AppArmor mediation blocking the map writes, unlike the aarch64 host this
    # module was developed against) turned into a reproducible regression: a
    # plain `ls /` under `_child_preexec` started failing with
    # `Permission denied` on `opendir("/")` specifically once the map writes
    # started succeeding, for a mechanism not fully root-caused before this
    # revision (#3030 PR discussion). Rather than ship an unexplained
    # interaction on a security-critical path, this drops the identity map
    # entirely: the process is left at the kernel's overflow uid/gid (65534,
    # "nobody") inside the namespace, which is the SAME state every host that
    # cannot write the map (e.g. the aarch64 host this was live-validated
    # against) already runs under, with no observed issue there. Network
    # isolation (the actual property this module exists for) is unaffected
    # either way — it is established by `unshare()` above, before this point.
    _logger.debug(
        "netns: no uid/gid identity map applied — os.getuid()/os.getgid() "
        "will report the namespace overflow id (65534) rather than the real "
        "identity in the sandboxed process; network isolation is unaffected"
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
