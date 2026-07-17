"""LandlockBackend — Linux 5.13+ filesystem and network restriction (FP-0017).

Uses the `landlock` PyPI package (ABI v1–v4) plus a seccomp-BPF stack for
syscall reduction. landlock is import-guarded — if the package or kernel
support is absent, `available()` returns False and the OS falls back to
NoopBackend (per FP-0017 auto-selection table).

Network restriction note: the py-landlock package (1.0.0.dev*) exposes no
network-port rule API — no `NetAccess`, no net symbol in `plumbing`, and
`Ruleset` has exactly `allow`/`apply` (measured on the pinned 1.0.0.dev5, not
read off the kernel's ABI table). So Landlock does NOT restrict outbound network
here, at ANY ABI. What carries a `network: false` policy is Linux
network-namespace isolation (`backends/netns.isolate_network_namespace`,
#3030): `_child_preexec` moves the child into a fresh, interface-less netns
before Landlock/seccomp apply, independent of `allow_subprocess` — the seccomp
filter's own network allowlist (`_NETWORK_SYSCALLS`, gated on `allow_subprocess`
same as before) is now defense-in-depth layered on top of the netns boundary,
not the boundary itself. The layer logs a one-shot WARN when Landlock's OWN
network gap is hit, so it stays diagnosable rather than implying Landlock
delivers a restriction it can't (#1693).

Filesystem model: allowlist-only. The HANDLED access set (Ruleset
``restrict_rules``) always governs the full write surface (write/make/remove) so
writes are denied-by-default and granted only to ``policy.write_paths``; reads +
exec are granted broadly on ``/``. Landlock cannot express a read deny-list, so
``policy.read_deny_paths`` is not enforced here (the network gate is the
exfiltration guard) — documented residual risk.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import subprocess

from .._subprocess_io import communicate_capped, kill_process_tree
from ..backend import SandboxResult, WrappedCommand
from ..policy import SandboxPolicy, expand_policy_path
from .seccomp import load_seccomp_filter, preload_native_dependency

_logger = logging.getLogger(__name__)

# One-shot warning latch for missing net restriction.
_NET_WARN_ISSUED = False


def build_ruleset(policy: SandboxPolicy, abi: int) -> object:
    """Build the Landlock ruleset for *policy* against the kernel ABI *abi*.

    THE one ruleset builder — both Landlock seams call it: ``run()`` (builds in
    the parent, ``apply()``s in a preexec_fn) and ``landlock_exec._apply_landlock``
    (builds and applies in the re-exec shim). It deliberately does not apply
    anything; who applies it, and in which process, is the caller's difference.

    Sharing it is the fix for #2980, not a tidy-up. The two seams used to build
    the ruleset separately, each carrying the same "Linux validation needed" TODO
    — and they drifted: the backend was ported to this real porcelain API
    (``Ruleset(restrict_rules=…)`` / ``.allow(path, rules=…)`` / ``.apply()``,
    #1693) while the shim went on calling ``add_path_beneath_rule`` /
    ``restrict_self`` / ``add_net_port_rule``, which the pinned
    ``landlock==1.0.0.dev5`` does not have (its ``Ruleset`` exposes exactly
    ``allow`` and ``apply`` — measured, not read). The shim therefore raised
    ``AttributeError`` before restricting anything, for 41 days, on a path no
    test drove. Two builders is what let one be right and the other fiction; one
    builder cannot drift from itself.

    Args:
        policy: the policy to translate into Landlock rules.
        abi: the kernel's supported Landlock ABI, from
            ``landlock.landlock_abi_version()`` (via ``available()``). Rights that
            exist only at a higher ABI are gated on it — an unsupported flag makes
            ruleset creation fail outright, so this is not optional.

    Returns:
        A ``landlock.Ruleset`` the caller applies.

    Raises:
        RuntimeError: if the ruleset cannot be built (a clear message beats a
            bare ctypes/attribute error at the seam).
    """
    import landlock  # noqa: PLC0415

    try:
        FS = landlock.FSAccess  # type: ignore[attr-defined]

        # Rights GRANTED on the broad-read surface: read files, list dirs, and
        # execute binaries (so the child can load /usr,/lib and exec the
        # target). Mirrors Seatbelt's broad ``(allow file-read*)`` +
        # ``(allow process-exec*)``.
        read_rules = FS.READ_FILE | FS.READ_DIR | FS.EXECUTE
        # Rights GRANTED on each write_path: the full create/modify/remove
        # surface; write implies read, so the read rights are included.
        write_rules = (
            read_rules
            | FS.WRITE_FILE
            | FS.MAKE_REG | FS.MAKE_DIR | FS.MAKE_SYM
            | FS.MAKE_CHAR | FS.MAKE_BLOCK | FS.MAKE_FIFO | FS.MAKE_SOCK
            | FS.REMOVE_FILE | FS.REMOVE_DIR
        )
        # REFER (cross-directory link/rename) is ABI 2+. Grant it on write_paths
        # so intra-sandbox moves between writable dirs work; gate it on the probed
        # ABI so neither the handled set nor a grant ever exceeds the kernel (an
        # unsupported flag would make ruleset creation fail).
        if abi >= 2:
            write_rules |= FS.REFER

        # HANDLED (governed) set: every access type the ruleset restricts. An
        # access type NOT handled is UNRESTRICTED (a hole), so we always handle
        # the full write surface — even with no write_paths — so writes are
        # governed (denied-by-default) and granted ONLY to write_paths. With no
        # write_paths that means no writes anywhere, which is exactly correct for
        # a sandbox (lead-confirmed secure default, #1693).
        handled = read_rules | write_rules

        # #1199 realignment — broad read surface. Landlock is allowlist-only
        # (path-beneath grants; anything not granted is denied), so broad-read is
        # a single read+exec grant on the filesystem root. This subsumes the old
        # per-path read allowlist (policy.read_paths) AND fixes the Linux gap
        # where system paths (/usr, /lib) had to be enumerated for binaries to
        # even load.
        #
        # Residual risk: Landlock CANNOT express a read deny-list (you cannot
        # carve a subpath out of an allowed parent), so policy.read_deny_paths is
        # NOT enforced here — unlike Seatbelt (SBPL deny-after-allow). The core
        # guarantee rests instead on the network gate: a compromised process on
        # Linux may read sensitive paths but cannot exfiltrate (network off unless
        # policy.network). This asymmetry is documented in the permission-model
        # residual-risk section.
        ruleset = landlock.Ruleset(restrict_rules=handled)  # type: ignore[attr-defined]
        ruleset.allow("/", rules=read_rules)
        for path in policy.write_paths:
            # expand_policy_path: same ``~`` contract as Seatbelt (#2976) — a
            # literal ``~`` path would be granted to a directory that does not
            # exist, silently leaving the intended write denied. No resolve():
            # Landlock hands the path to the kernel as-is.
            ruleset.allow(str(expand_policy_path(path)), rules=write_rules)

        # Network: the py-landlock package exposes no net-port rule API at ANY
        # ABI, so Landlock CANNOT restrict outbound network here — the deny is
        # netns's (`_child_preexec` isolates the child into a fresh network
        # namespace before this ruleset applies, #3030). Warn once so the gap in
        # THIS layer specifically is diagnosable rather than faking an
        # enforcement it can't deliver (documented residual risk, #1693). See
        # `_warn_net_once` for what this message used to claim and why each part
        # of it was false.
        if not policy.network:
            _warn_net_once()

        return ruleset
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"LandlockBackend: failed to build Landlock ruleset: {exc}"
        ) from exc


def _child_preexec(ruleset: object | None, policy: SandboxPolicy) -> None:
    """Apply netns + Landlock + seccomp restrictions in the child process
    (preexec_fn).

    Called after fork(), before exec(). Network-namespace isolation (#3030)
    goes before the seccomp step (``unshare`` is not in the seccomp allowlist,
    so it would be refused afterward); it writes no ``/proc/self/*`` map, so it
    has no ordering dependency on Landlock. Unlike the Landlock/seccomp steps
    below, a netns failure is NOT swallowed: it RAISES, which
    ``subprocess.Popen`` propagates to the parent as a ``SubprocessError`` —
    fail-closed, because running the target with network reachable when the
    policy denied it is the exact defect #3030 is.

    The Landlock ruleset is built in the parent (its ruleset fd survives the
    fork); ``apply()`` issues ``landlock_restrict_self`` on the calling (child)
    thread and is irrevocable for the rest of that process's lifetime. The
    seccomp filter is loaded after it and is likewise irrevocable, and survives
    the execve that follows.

    Module-level (rather than a closure inside ``run``) so the seccomp wiring
    below is reachable by a test without a Linux host: ``ruleset=None`` means
    "no Landlock ruleset to apply" and skips straight to the seccomp step.
    Production always passes a real ruleset.
    """
    if not policy.network:
        from .netns import isolate_network_namespace

        isolate_network_namespace()  # raises -> propagates as SubprocessError

    if ruleset is not None:
        try:
            ruleset.apply()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            # If apply() fails (e.g., kernel too old despite probe), log and
            # continue — the process will run without Landlock enforcement
            # rather than silently failing to start.
            import sys  # noqa: PLC0415

            print(  # noqa: T201
                f"LandlockBackend: ruleset.apply() failed: {exc}",
                file=sys.stderr,
            )

    if not policy.allow_subprocess:
        # Loads immediately in this (child) process and survives the execve that
        # Popen issues next. Note this runs BEFORE CPython's own post-preexec_fn
        # code, whose syscalls are filtered too — hence `close_range` in the
        # baseline, which the landlock_exec shim's plain-execvp shape never needs.
        #
        # This gate is the SIBLING of `landlock_exec._apply_seccomp`'s: it still
        # skips the whole filter (including its own network allowlist) when
        # `allow_subprocess` is True. That no longer leaves `network: false`
        # unenforced (#3030) — the netns step above is the actual network
        # boundary and does not depend on this gate at all. Whether the FORK
        # gate belongs on the whole filter or only on the allowlist contents
        # remains #2962's open, un-fixed design question.
        #
        # The import must already be warm: pyseccomp resolves native libs at
        # import via the filesystem, which Landlock has just closed above. `run()`
        # warms it in the parent before the fork; this child inherits it (#3020).
        load_seccomp_filter(policy)


def _preexec_failure_message(exc: Exception, policy: SandboxPolicy) -> str:
    """Build the stderr text for a ``Popen`` failure raised out of
    ``_child_preexec``.

    ``subprocess.Popen`` re-raises a ``preexec_fn`` exception in the parent as
    a generic ``subprocess.SubprocessError("Exception occurred in
    preexec_fn.")`` — the original message (e.g. the errno detail from
    ``isolate_network_namespace``) does not survive the round trip (measured:
    Python's ``_posixsubprocess`` error-pipe protocol reports the class of
    failure, not the instance). ``_child_preexec`` raises from exactly one
    place when ``policy.network`` is False, so a ``SubprocessError`` under that
    policy is attributable to the netns step without needing the swallowed
    detail; an ``OSError`` (e.g. the executable itself could not be found) is
    passed through unchanged.
    """
    if isinstance(exc, subprocess.SubprocessError) and not policy.network:
        return (
            "sandbox refused to run: network-namespace isolation failed in the "
            "child before exec (policy.network=False requires it on Linux; the "
            "host cannot deliver a network deny for this run — see #3030)"
        )
    return str(exc)


def _warn_net_once() -> None:
    """Warn once that this policy's network deny is not Landlock's to deliver.

    The message used to blame the kernel ABI ("requires ABI >= 4 (Linux 6.7+);
    current ABI=%d"), which was false and, on an ABI-4 host, self-refuting — it
    printed "requires ABI >= 4 … current ABI=4" and still declined to restrict.
    An operator reading it would go upgrade a kernel that was never the problem.
    Measured on the pinned `landlock==1.0.0.dev5`: the package exposes no network
    API at ALL — no `NetAccess`, no net symbol anywhere in `plumbing`, and
    `Ruleset` has exactly `allow`/`apply` — so no kernel ABI makes this reachable.
    It is a package gap, and the message now says so.

    It also used to point at "the no-network-fd / proxy gate" as the mechanism
    that delivers the deny instead. That gate does not exist: the phrase appears
    nowhere in this repo outside this module's own comments. #3030 initially
    named the seccomp filter's default-deny as the real mechanism instead — also
    wrong in general, since that filter is skipped entirely when
    `allow_subprocess` is True (the stdio-MCP default), which is exactly what let
    a real outbound connect+send SUCCEED under `network=False,
    allow_subprocess=True`. The actual mechanism, since #3030's fix, is
    `backends.netns.isolate_network_namespace` — independent of
    `allow_subprocess` and not a syscall-name boundary at all. This WARN exists
    only because THIS layer (Landlock itself) still cannot restrict network; the
    message names netns as what actually does.

    All three corrections are #2980's class — a claim about what the installed
    code does, inferred rather than measured.
    """
    global _NET_WARN_ISSUED
    if _NET_WARN_ISSUED:
        return
    _NET_WARN_ISSUED = True
    _logger.warning(
        "LandlockBackend: this policy denies network, but Landlock will NOT "
        "restrict outbound network here — the pinned `landlock` package exposes "
        "no network-rule API on any kernel ABI (upgrading the kernel does not "
        "change this). The deny is instead carried by moving the sandboxed "
        "process into a fresh, interface-less network namespace before it runs "
        "(see issue #3030), independent of allow_subprocess."
    )


class LandlockBackend:
    """Linux Landlock LSM backend (FP-0017 Component B).

    Applies filesystem path-beneath rules before exec via
    ``landlock.Ruleset(restrict_rules=…).allow(path, rules=…)`` built in the
    parent, then ``ruleset.apply()`` in a preexec_fn. The restriction is
    irrevocable within the child process. (Network is enforced via a Linux
    network namespace applied in the same preexec_fn, not by Landlock itself —
    the py-landlock package has no net-port rule API; see module docstring.)
    """

    name: str = "landlock"

    def __init__(self) -> None:
        self._abi_version: int | None = None  # populated on first available() call
        self._import_error: str | None = None
        self._available: bool | None = None  # cached result

    @property
    def abi_version(self) -> int | None:
        """The kernel's supported Landlock ABI, or None before the first
        ``available()`` call has probed for it. Public because the ruleset build
        is ABI-gated and :mod:`reyn.security.sandbox.landlock_exec` builds one
        through :func:`build_ruleset` too — the shim needs the same number this
        backend does, and reaching into ``_abi_version`` for it is what a shared
        builder exists to avoid."""
        return self._abi_version

    @property
    def import_error(self) -> str | None:
        """Read-only accessor for the cached ImportError message, or None
        if the landlock dependency was importable. Set on first
        ``available()`` call when the import fails."""
        return self._import_error

    def self_test(self) -> str | None:
        """Witness real denies through the Landlock wrap (#2983): None when a
        write outside ``write_paths`` was refused AND a spawn under
        ``allow_subprocess=False`` was refused, else the reason one was not.
        Cached per process; see ``reyn.security.sandbox.self_test``.

        The second axis is seccomp's, not Landlock's (``_child_preexec`` /
        ``landlock_exec._apply_seccomp`` gate ``fork``/``clone`` on
        ``allow_subprocess``), and it is why passing the write probe was never
        evidence about this backend's syscall layer: the write boundary is
        Landlock's alone, so #2962 — a filter that never loaded — is invisible to
        it.

        Note both probes go through ``wrap_command`` — the re-exec shim — which is
        where #2980 lives (the shim calls ``Ruleset`` methods the pinned package
        does not have, so it raises before restricting anything). The shim reaches
        ``available()`` on its own side; it never reaches back into this method,
        so the probe cannot recurse into itself.
        """
        from ..self_test import enforcement_self_test  # noqa: PLC0415

        return enforcement_self_test(self)

    def available(self) -> bool:
        """Return True iff the Landlock mechanism is PRESENT on this platform.

        Presence only — Linux + the package imports + kernel ABI >= 1. That is
        exactly the check #2980 passed while the shim was unreachable, which is
        why ``self_test()`` (does a deny actually fire) is a separate question.

        Caches the result after the first call so repeated invocations are O(1).
        """
        if self._available is not None:
            return self._available

        # Must be Linux.
        if platform.system() != "Linux":
            self._available = False
            return False

        # Try importing the landlock package (importlib so the import attempt is
        # a single interceptable call — the availability result is cached below).
        try:
            import importlib

            landlock = importlib.import_module("landlock")
        except ImportError as exc:
            self._import_error = str(exc)
            self._available = False
            return False

        # Probe the kernel's supported Landlock ABI via the package's
        # module-level ``landlock_abi_version()`` (the real py-landlock API). It
        # returns the best ABI the running kernel supports, or <= 0 when Landlock
        # is unavailable (kernel too old / disabled). <1 → fall back to Noop.
        try:
            self._abi_version = int(landlock.landlock_abi_version())
        except Exception as exc:  # noqa: BLE001
            _logger.debug("LandlockBackend ABI probe failed: %s", exc)
            self._available = False
            return False

        if self._abi_version < 1:
            self._available = False
            return False

        self._available = True
        return True

    def wrap_command(self, argv: list[str], policy: SandboxPolicy) -> WrappedCommand:
        """Prepend the ``landlock_exec`` re-exec shim to *argv* for a
        persistent-process launch (e.g. a stdio MCP server, #1344 follow-up E).
        Landlock has no CLI wrapper, so the shim (a re-exec-and-restrict-self
        module) IS the command-level wrap — the COMMAND-level analog of the
        Seatbelt ``sandbox-exec -f <profile>`` wrap. No cleanup resource is
        owned (unlike Seatbelt's temp profile)."""
        if not argv:
            raise ValueError("wrap_command: argv must be non-empty (command + args)")
        from ..landlock_exec import build_landlock_exec_argv

        executable, shim_argv = build_landlock_exec_argv(policy, argv[0], list(argv[1:]))
        return WrappedCommand(argv=[executable, *shim_argv], cleanup=None)

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SandboxResult:
        """Execute argv under Landlock isolation and return the result.

        ``cwd`` (= the run's ``workspace.base_dir``) sets the child working
        directory so repo-relative ``git`` / ``pytest`` resolve correctly; the
        Landlock ruleset still bounds filesystem access independently.
        """
        if not self.available():
            raise RuntimeError(
                "LandlockBackend not available on this platform. "
                "Requires Linux 5.13+ with the `landlock` package installed."
            )

        abi = self._abi_version or 0  # guaranteed non-None after available() == True

        # Build env from the passthrough allowlist (mirrors NoopBackend exactly).
        env: dict[str, str] = {}
        for name in policy.env_passthrough:
            if name in os.environ:
                env[name] = os.environ[name]
        if "PATH" not in env and "PATH" in os.environ:
            env["PATH"] = os.environ["PATH"]

        # THE shared builder — the same call the landlock_exec shim makes, so the
        # two seams cannot drift apart again (#2980).
        ruleset = build_ruleset(policy, abi)

        # Resolve pyseccomp's native libraries HERE, in the PARENT, before the
        # fork. `_child_preexec` loads the filter in a child that Landlock has
        # already restricted, where the import's own filesystem work is denied —
        # so deferring it to the child made the filter load only when something
        # else in the parent happened to have imported pyseccomp first (#3020,
        # measured: without this the child died in preexec_fn). The import is
        # inherited across fork, so warming it here is what makes the child's load
        # a pure in-memory operation. Gated exactly as `_child_preexec` gates the
        # load, so a subprocess-permitting policy pays nothing.
        if not policy.allow_subprocess:
            preload_native_dependency()

        loop = asyncio.get_running_loop()

        if cancel_event is None:
            # No cancel support: original blocking path (byte-identical).
            def _run_blocking() -> SandboxResult:
                try:
                    proc = subprocess.Popen(
                        argv,
                        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        cwd=cwd,
                        start_new_session=True,
                        preexec_fn=lambda: _child_preexec(ruleset, policy),
                    )
                    try:
                        stdout_b, stderr_b, truncated = communicate_capped(
                            proc,
                            input=stdin,
                            max_bytes=policy.max_output_bytes,
                            timeout=policy.timeout_seconds,
                        )
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        stdout_b, stderr_b, _trunc = communicate_capped(
                            proc, max_bytes=policy.max_output_bytes
                        )
                        return SandboxResult(
                            returncode=-1,
                            stdout=stdout_b or b"",
                            stderr=(stderr_b or b"")
                            + f"\nCommand timed out after {policy.timeout_seconds}s".encode(),
                            truncated=_trunc,
                        )
                    return SandboxResult(
                        returncode=proc.returncode,
                        stdout=stdout_b or b"",
                        stderr=stderr_b or b"",
                        truncated=truncated,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    return SandboxResult(
                        returncode=-1,
                        stdout=b"",
                        stderr=_preexec_failure_message(exc, policy).encode(),
                        truncated=False,
                    )

            return await loop.run_in_executor(None, _run_blocking)

        # #1470: cancel-aware path — Popen in executor + asyncio.wait race.
        # ⚠ Linux-only; logic mirrors SeatbeltBackend (verified on macOS) — the
        # cancel block + `kill_process_tree` (SIGTERM-pg → SIGKILL grace) are a
        # faithful mirror (code-inspected, #1527). LIVE-confirmed by
        # ``tests/test_subprocess_cancel_1470.py::test_landlock_cancel_kills_subprocess``
        # when run on a Linux 5.13+ host with the landlock LSM (skipif-gated; the
        # CI Noop default + macOS e2e don't exercise it). Worst-case if cancel does
        # not work: behaviour degrades to pre-#1470 (subprocess runs to completion)
        # — no regression beyond the cooperative-cancel latency that already existed.
        try:
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=cwd,
                    start_new_session=True,
                    preexec_fn=lambda: _child_preexec(ruleset, policy),
                ),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return SandboxResult(
                returncode=-1, stdout=b"", stderr=_preexec_failure_message(exc, policy).encode()
            )

        if stdin is not None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except OSError:
                pass

        comm_future: asyncio.Future = loop.run_in_executor(
            None, lambda: communicate_capped(proc, max_bytes=policy.max_output_bytes)
        )
        cancel_task = asyncio.create_task(cancel_event.wait())

        done, _ = await asyncio.wait(
            {comm_future, cancel_task},
            timeout=policy.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if cancel_task in done:
            await kill_process_tree(proc)
            cancel_task.cancel()
            try:
                stdout_b, stderr_b, _trunc = await asyncio.wait_for(
                    asyncio.shield(comm_future), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception):
                stdout_b, stderr_b, _trunc = b"", b"", False
            return SandboxResult(
                returncode=-int(signal.SIGTERM),
                stdout=stdout_b or b"",
                stderr=stderr_b or b"",
                truncated=_trunc,
                cancelled=True,
            )
        elif not done:
            cancel_task.cancel()
            await kill_process_tree(proc)
            try:
                stdout_b, stderr_b, _trunc = await asyncio.wait_for(
                    asyncio.shield(comm_future), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception):
                stdout_b, stderr_b, _trunc = b"", b"", False
            return SandboxResult(
                returncode=-1,
                stdout=stdout_b or b"",
                stderr=(stderr_b or b"")
                + f"\nCommand timed out after {policy.timeout_seconds}s".encode(),
                truncated=_trunc,
            )
        else:
            cancel_task.cancel()
            stdout_b, stderr_b, _trunc = await comm_future
            return SandboxResult(
                returncode=proc.returncode,
                stdout=stdout_b or b"",
                stderr=stderr_b or b"",
                truncated=_trunc,
            )
