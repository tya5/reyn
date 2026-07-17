"""NoopBackend — fallback that runs commands with NO isolation enforcement.

This backend exists so the `sandboxed_exec` op contract works on every
platform; it does NOT provide real sandboxing. On macOS the future
SeatbeltBackend (FP-0017 Component C) and on Linux the future
LandlockBackend (Component B) will replace this default.

The first invocation emits a one-line WARN so operators know they are not
getting enforcement.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess

from ._subprocess_io import communicate_capped, kill_process_tree
from .backend import SandboxBackend, SandboxResult, WrappedCommand
from .policy import SandboxPolicy

_logger = logging.getLogger(__name__)

_NOOP_WARNING_ISSUED = False


def _warn_once() -> None:
    """Emit the one-line WARN exactly once per process."""
    global _NOOP_WARNING_ISSUED
    if _NOOP_WARNING_ISSUED:
        return
    _NOOP_WARNING_ISSUED = True
    _logger.warning(
        "Sandbox is in noop mode — no isolation enforced. "
        "Install SeatbeltBackend (macOS) or LandlockBackend (Linux) for real enforcement."
    )


def _reset_warning_for_tests() -> None:
    """Test hook: reset the one-shot warning latch."""
    global _NOOP_WARNING_ISSUED
    _NOOP_WARNING_ISSUED = False


def _build_env(policy: SandboxPolicy) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in policy.env_passthrough:
        if name in os.environ:
            env[name] = os.environ[name]
    if "PATH" not in env and "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
    return env


class NoopBackend:
    """Always-available passthrough backend.

    Honors `policy.timeout_seconds` (wall-clock cap) and `policy.env_passthrough`
    (env-var allowlist). All other policy fields are recorded for audit only —
    NoopBackend does not enforce them.

    #1470: when cancel_event is provided and set, kills the subprocess via
    process-group SIGTERM → SIGKILL and returns SandboxResult(cancelled=True).
    """

    name: str = "noop"

    def available(self) -> bool:
        return True

    def self_test(self) -> str | None:
        """Always None — NoopBackend is EXEMPT from the enforcement self-test
        (#2983), and is the only backend that is.

        The self-test exists to catch a backend that CLAIMS enforcement it does
        not deliver. NoopBackend claims none: "no isolation enforced" is its
        documented contract, it says so in a WARN on first use, and
        `get_default_backend()` never selects it while a real backend is working.
        Its `available()` means "this passthrough will run your command", not
        "this will contain it" — so there is no false claim here to falsify.

        The decisive reason, though, is structural rather than semantic: Noop is
        the TARGET of the ``on_unsupported`` fallback. A failing self-test here
        would demand falling back from Noop to Noop — an infinite regress with no
        floor beneath it. The one backend that must never be self-tested is the
        one every failed self-test lands on.

        This exemption is not a hole. `probe_enforcement()` pointed at this very
        backend is what proves the probe can fail at all (see
        `tests/test_sandbox_self_test_2983.py`), and CodeAct independently
        refuses to run on a backend named "noop" (`codeact_runner.py`), so the
        exemption grants Noop no enforcement credit anywhere.
        """
        return None

    def wrap_command(self, argv: list[str], policy: SandboxPolicy) -> WrappedCommand:
        """Passthrough: argv is returned UNCHANGED — no enforcement — but the
        call still went THROUGH the sandbox abstraction (the owner-acceptable
        no-isolation case, #2620), as opposed to a caller that never consulted
        any backend at all."""
        _warn_once()
        return WrappedCommand(argv=list(argv), cleanup=None)

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SandboxResult:
        _warn_once()

        env = _build_env(policy)

        if cancel_event is None:
            # No cancel support: original blocking path (byte-identical).
            loop = asyncio.get_running_loop()

            def _run_blocking() -> SandboxResult:
                try:
                    proc = subprocess.Popen(
                        argv,
                        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        cwd=cwd,
                    )
                except OSError as exc:
                    return SandboxResult(
                        returncode=-1, stdout=b"", stderr=str(exc).encode()
                    )
                try:
                    stdout_b, stderr_b, truncated = communicate_capped(
                        proc,
                        input=stdin,
                        max_bytes=policy.max_output_bytes,
                        timeout=policy.timeout_seconds,
                    )
                    return SandboxResult(
                        returncode=proc.returncode,
                        stdout=stdout_b,
                        stderr=stderr_b,
                        truncated=truncated,
                    )
                except subprocess.TimeoutExpired as exc:
                    proc.kill()
                    proc.wait()
                    stdout_b = exc.stdout if isinstance(exc.stdout, bytes) else b""
                    stderr_b = exc.stderr if isinstance(exc.stderr, bytes) else b""
                    return SandboxResult(
                        returncode=-1,
                        stdout=stdout_b,
                        stderr=stderr_b
                        + f"\nCommand timed out after {policy.timeout_seconds}s".encode(),
                    )

            return await loop.run_in_executor(None, _run_blocking)

        # #1470: cancel-aware path — Popen with process group + asyncio.wait race.
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except OSError as exc:
            return SandboxResult(returncode=-1, stdout=b"", stderr=str(exc).encode())

        if stdin is not None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except OSError:
                pass

        loop = asyncio.get_running_loop()
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
            # cancel_inflight() fired: kill process group + return partial output.
            await kill_process_tree(proc)
            cancel_task.cancel()
            # Read whatever output was captured before the kill.
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
            # Timeout: kill and return with timeout marker.
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
            # Normal completion.
            cancel_task.cancel()
            stdout_b, stderr_b, _trunc = await comm_future
            return SandboxResult(
                returncode=proc.returncode,
                stdout=stdout_b or b"",
                stderr=stderr_b or b"",
                truncated=_trunc,
            )
