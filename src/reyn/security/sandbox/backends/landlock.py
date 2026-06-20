"""LandlockBackend — Linux 5.13+ filesystem and network restriction (FP-0017).

Uses the `landlock` PyPI package (ABI v1–v4) plus a seccomp-BPF stack for
syscall reduction. landlock is import-guarded — if the package or kernel
support is absent, `available()` returns False and the OS falls back to
NoopBackend (per FP-0017 auto-selection table).

Network restriction note: the py-landlock package (1.0.0.dev*) exposes no
network-port rule API, so Landlock does NOT restrict outbound network here. When
a policy denies network, that guarantee comes from a different mechanism (the
no-network-fd / proxy gate); the Landlock layer logs a one-shot WARN so the gap
is diagnosable rather than faking enforcement it can't deliver (#1693).

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

from .._subprocess_io import communicate_capped
from ..backend import SandboxResult
from ..policy import SandboxPolicy

_logger = logging.getLogger(__name__)

# One-shot warning latch for missing net restriction.
_NET_WARN_ISSUED = False


def _warn_net_once(abi: int) -> None:
    global _NET_WARN_ISSUED
    if _NET_WARN_ISSUED:
        return
    _NET_WARN_ISSUED = True
    _logger.warning(
        "LandlockBackend: network restriction requires ABI >= 4 (Linux 6.7+); "
        "current ABI=%d — outbound network will NOT be restricted.",
        abi,
    )


class LandlockBackend:
    """Linux Landlock LSM backend (FP-0017 Component B).

    Applies filesystem path-beneath rules before exec via
    ``landlock.Ruleset(restrict_rules=…).allow(path, rules=…)`` built in the
    parent, then ``ruleset.apply()`` in a preexec_fn. The restriction is
    irrevocable within the child process. (Network is not enforced at this layer
    — the py-landlock package has no net-port rule API; see module docstring.)
    """

    name: str = "landlock"

    def __init__(self) -> None:
        self._abi_version: int | None = None  # populated on first available() call
        self._import_error: str | None = None
        self._available: bool | None = None  # cached result

    @property
    def import_error(self) -> str | None:
        """Read-only accessor for the cached ImportError message, or None
        if the landlock dependency was importable. Set on first
        ``available()`` call when the import fails."""
        return self._import_error

    def available(self) -> bool:
        """Return True iff Landlock is usable on this platform.

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

        abi = self._abi_version  # guaranteed non-None after available() == True

        # Build the Landlock ruleset against the real py-landlock porcelain API
        # (landlock 1.0.0.dev*): ``Ruleset(restrict_rules=FSAccess…)`` declares the
        # HANDLED (governed) access set, ``.allow(path, rules=FSAccess…)`` grants
        # rights on a path-beneath, ``.apply()`` enforces irrevocably (#1693).
        import landlock  # noqa: PLC0415

        # Build env from the passthrough allowlist (mirrors NoopBackend exactly).
        env: dict[str, str] = {}
        for name in policy.env_passthrough:
            if name in os.environ:
                env[name] = os.environ[name]
        if "PATH" not in env and "PATH" in os.environ:
            env["PATH"] = os.environ["PATH"]

        # Lazy-import seccomp integration from sibling module (FP-0017 Component B).
        try:
            from .seccomp import install_seccomp_filter  # noqa: PLC0415
        except ImportError:
            install_seccomp_filter = None  # type: ignore[assignment]

        def _build_preexec(ruleset: object) -> None:
            """Apply Landlock restrictions in the child process (preexec_fn).

            Called after fork(), before exec(). The ruleset is built in the parent
            (its ruleset fd survives the fork); ``apply()`` issues
            ``landlock_restrict_self`` on the calling (child) thread and is
            irrevocable for the rest of that process's lifetime.
            """
            try:
                ruleset.apply()  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                # If apply() fails (e.g., kernel too old despite probe), log and
                # continue — the process will run without Landlock enforcement
                # rather than silently failing to start.
                import sys  # noqa: PLC0415

                print(  # noqa: T201
                    f"LandlockBackend: ruleset.apply() failed: {exc}",
                    file=sys.stderr,
                )

            if install_seccomp_filter is not None and not policy.allow_subprocess:
                install_seccomp_filter(policy)

        # Build the ruleset (real py-landlock porcelain API).
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
            # REFER (cross-directory link/rename) is ABI 2+. Grant it on
            # write_paths so intra-sandbox moves between writable dirs work; gate it
            # on the probed ABI so neither the handled set nor a grant ever exceeds
            # the kernel (an unsupported flag would make ruleset creation fail).
            if (abi or 0) >= 2:
                write_rules |= FS.REFER

            # HANDLED (governed) set: every access type the ruleset restricts. An
            # access type NOT handled is UNRESTRICTED (a hole), so we always handle
            # the full write surface — even with no write_paths — so writes are
            # governed (denied-by-default) and granted ONLY to write_paths. With no
            # write_paths that means no writes anywhere, which is exactly correct
            # for a sandbox (lead-confirmed secure default, #1693).
            handled = read_rules | write_rules

            # #1199 realignment — broad read surface. Landlock is allowlist-only
            # (path-beneath grants; anything not granted is denied), so broad-read
            # is a single read+exec grant on the filesystem root. This subsumes the
            # old per-path read allowlist (policy.read_paths) AND fixes the Linux
            # gap where system paths (/usr, /lib) had to be enumerated for binaries
            # to even load.
            #
            # Residual risk: Landlock CANNOT express a read deny-list (you cannot
            # carve a subpath out of an allowed parent), so policy.read_deny_paths
            # is NOT enforced here — unlike Seatbelt (SBPL deny-after-allow). The
            # core guarantee rests instead on the network gate: a compromised
            # process on Linux may read sensitive paths but cannot exfiltrate
            # (network off unless policy.network). This asymmetry is documented in
            # the permission-model residual-risk section.
            ruleset = landlock.Ruleset(restrict_rules=handled)  # type: ignore[attr-defined]
            ruleset.allow("/", rules=read_rules)
            for path in policy.write_paths:
                ruleset.allow(path, rules=write_rules)

            # Network: the py-landlock package exposes no net-port rule API at this
            # ABI, so Landlock CANNOT restrict outbound network here. When the
            # policy denies network, that guarantee is delivered by a DIFFERENT
            # mechanism (the no-network-fd / proxy gate), not Landlock — warn once
            # so the gap is diagnosable rather than faking an enforcement we can't
            # deliver (documented residual risk, #1693).
            if not policy.network:
                _warn_net_once(abi or 0)

        except Exception as exc:  # noqa: BLE001
            # If ruleset construction fails, surface a clear error.
            raise RuntimeError(
                f"LandlockBackend: failed to build Landlock ruleset: {exc}"
            ) from exc

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
                        preexec_fn=lambda: _build_preexec(ruleset),
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
                except OSError as exc:
                    return SandboxResult(
                        returncode=-1,
                        stdout=b"",
                        stderr=str(exc).encode(),
                        truncated=False,
                    )

            return await loop.run_in_executor(None, _run_blocking)

        # #1470: cancel-aware path — Popen in executor + asyncio.wait race.
        # ⚠ Linux-only; logic mirrors SeatbeltBackend (verified on macOS) — the
        # cancel block + `_kill_proc_group` (SIGTERM-pg → SIGKILL grace) are a
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
                    preexec_fn=lambda: _build_preexec(ruleset),
                ),
            )
        except OSError as exc:
            return SandboxResult(returncode=-1, stdout=b"", stderr=str(exc).encode())

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
            await _kill_proc_group(proc, loop)
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
            await _kill_proc_group(proc, loop)
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


async def _kill_proc_group(
    proc: subprocess.Popen, loop: asyncio.AbstractEventLoop, grace_seconds: float = 2.0
) -> None:
    """SIGTERM the process group, then SIGKILL after grace_seconds if still alive."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, proc.wait), timeout=grace_seconds,
        )
    except (asyncio.TimeoutError, Exception):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
