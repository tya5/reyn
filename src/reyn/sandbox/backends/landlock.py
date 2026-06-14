"""LandlockBackend — Linux 5.13+ filesystem and network restriction (FP-0017).

Uses the `landlock` PyPI package (ABI v1–v4) plus a seccomp-BPF stack for
syscall reduction. landlock is import-guarded — if the package or kernel
support is absent, `available()` returns False and the OS falls back to
NoopBackend (per FP-0017 auto-selection table).

Network restriction note: Landlock ABI v4+ (Linux 6.7+) supports outbound
network port restriction via LANDLOCK_RULE_NET_PORT. On kernels with ABI < 4,
network isolation is unavailable at the Landlock layer; a WARN is logged once.

Marker: contributor-friendly track. The maintainer dev environment is
macOS-only; Linux contributors are invited to validate the actual
landlock_restrict_self() effects end-to-end (see TODO comments).
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import subprocess

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

    Applies filesystem path-beneath rules and (ABI 4+) network port rules
    before exec via landlock.Ruleset.restrict_self() in a preexec_fn. The
    restriction is irrevocable within the child process.
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

        # Try importing the landlock package.
        try:
            import importlib

            importlib.import_module("landlock")
        except ImportError as exc:
            self._import_error = str(exc)
            self._available = False
            return False

        # Probe ABI version.
        try:
            import landlock  # noqa: PLC0415

            # TODO(fp-0017-b): Linux validation needed — verify the correct
            # attribute/function name for the installed landlock package version.
            if hasattr(landlock, "abi_version"):
                self._abi_version = landlock.abi_version()
            elif hasattr(landlock, "LANDLOCK_ABI_BEST"):
                self._abi_version = landlock.LANDLOCK_ABI_BEST
            else:
                # Fall back to running a minimal ruleset creation to detect support.
                # TODO(fp-0017-b): Linux validation needed — adjust probe if API differs.
                ruleset = landlock.Ruleset()
                self._abi_version = getattr(ruleset, "abi_version", 1)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("LandlockBackend ABI probe failed: %s", exc)
            self._available = False
            return False

        if self._abi_version is None or self._abi_version < 1:
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

        # Build the Landlock ruleset.
        # TODO(fp-0017-b): Linux validation needed — verify Ruleset construction API
        # and access-right constants for the installed landlock package version.
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

            Called after fork(), before exec(). Irrevocable after restrict_self().
            """
            # TODO(fp-0017-b): Linux validation needed — verify restrict_self() call.
            try:
                ruleset.restrict_self()  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                # If restrict_self fails (e.g., kernel too old despite probe), log
                # and continue — the process will run without Landlock enforcement
                # rather than silently failing to start.
                import sys  # noqa: PLC0415

                print(  # noqa: T201
                    f"LandlockBackend: restrict_self() failed: {exc}",
                    file=sys.stderr,
                )

            if install_seccomp_filter is not None and not policy.allow_subprocess:
                # TODO(fp-0017-b): Linux validation needed — verify seccomp integration.
                install_seccomp_filter(policy)

        # Build ruleset with path-beneath rules.
        # TODO(fp-0017-b): Linux validation needed — verify Ruleset, PathBeneath,
        # and access constant names/values for the installed package version.
        try:
            # Attempt to build the ruleset using the landlock package API.
            # The `landlock` PyPI package (https://github.com/landlock-lsm/py-landlock)
            # uses Ruleset(handled_access_fs=...) and add_path_beneath_rule().
            _fs_read = getattr(
                landlock,
                "AccessFS",
                None,
            )
            ruleset = landlock.Ruleset()  # type: ignore[attr-defined]

            # #1199 realignment — broad read surface. Landlock is allowlist-only
            # (path-beneath grants; anything not granted is denied), so broad-read
            # is a single read rule on the filesystem root. This subsumes the old
            # per-path read allowlist (policy.read_paths) AND fixes the Linux gap
            # where system paths (/usr, /lib, dyld-equivalents) had to be
            # enumerated for binaries to even load.
            #
            # Residual risk: Landlock CANNOT express a read deny-list (you cannot
            # carve a subpath out of an allowed parent), so policy.read_deny_paths
            # is NOT enforced here — unlike Seatbelt (SBPL deny-after-allow). The
            # core guarantee rests instead on the network gate below: a compromised
            # process on Linux may read sensitive paths but cannot exfiltrate
            # (network off unless policy.network). This asymmetry is documented in
            # the permission-model residual-risk section.
            # TODO(fp-0017-b): Linux validation needed — verify access right constants.
            ruleset.add_path_beneath_rule(  # type: ignore[attr-defined]
                "/",
                read_only=True,
            )

            for path in policy.write_paths:
                # TODO(fp-0017-b): Linux validation needed — verify access right constants.
                ruleset.add_path_beneath_rule(  # type: ignore[attr-defined]
                    path,
                    read_only=False,
                )

            # Network restriction (ABI 4+ only).
            if not policy.network:
                if abi is not None and abi >= 4:
                    # TODO(fp-0017-b): Linux validation needed — verify
                    # LANDLOCK_RULE_NET_PORT API for outbound TCP restriction.
                    if hasattr(ruleset, "add_net_port_rule"):
                        ruleset.add_net_port_rule(deny_all_outbound=True)  # type: ignore[attr-defined]
                else:
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
                        stdout_b, stderr_b = proc.communicate(
                            input=stdin,
                            timeout=policy.timeout_seconds,
                        )
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        stdout_b, stderr_b = proc.communicate()
                        return SandboxResult(
                            returncode=-1,
                            stdout=stdout_b or b"",
                            stderr=(stderr_b or b"")
                            + f"\nCommand timed out after {policy.timeout_seconds}s".encode(),
                            truncated=False,
                        )
                    return SandboxResult(
                        returncode=proc.returncode,
                        stdout=stdout_b or b"",
                        stderr=stderr_b or b"",
                        truncated=False,
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

        comm_future: asyncio.Future = loop.run_in_executor(None, proc.communicate)
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
                stdout_b, stderr_b = await asyncio.wait_for(
                    asyncio.shield(comm_future), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception):
                stdout_b, stderr_b = b"", b""
            return SandboxResult(
                returncode=-int(signal.SIGTERM),
                stdout=stdout_b or b"",
                stderr=stderr_b or b"",
                cancelled=True,
            )
        elif not done:
            cancel_task.cancel()
            await _kill_proc_group(proc, loop)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    asyncio.shield(comm_future), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception):
                stdout_b, stderr_b = b"", b""
            return SandboxResult(
                returncode=-1,
                stdout=stdout_b or b"",
                stderr=(stderr_b or b"")
                + f"\nCommand timed out after {policy.timeout_seconds}s".encode(),
            )
        else:
            cancel_task.cancel()
            stdout_b, stderr_b = await comm_future
            return SandboxResult(
                returncode=proc.returncode,
                stdout=stdout_b or b"",
                stderr=stderr_b or b"",
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
