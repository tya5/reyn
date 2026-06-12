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

from .backend import SandboxBackend, SandboxResult
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


async def _kill_proc_group(proc: subprocess.Popen, grace_seconds: float = 2.0) -> None:
    """SIGTERM the process group, then SIGKILL after grace_seconds if still alive."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, proc.wait),
            timeout=grace_seconds,
        )
    except (asyncio.TimeoutError, Exception):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


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
                    completed = subprocess.run(
                        argv,
                        input=stdin,
                        capture_output=True,
                        env=env,
                        cwd=cwd,
                        timeout=policy.timeout_seconds,
                        check=False,
                    )
                    return SandboxResult(
                        returncode=completed.returncode,
                        stdout=completed.stdout or b"",
                        stderr=completed.stderr or b"",
                        truncated=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    stdout_b = exc.stdout if isinstance(exc.stdout, bytes) else b""
                    stderr_b = exc.stderr if isinstance(exc.stderr, bytes) else b""
                    return SandboxResult(
                        returncode=-1,
                        stdout=stdout_b,
                        stderr=stderr_b
                        + f"\nCommand timed out after {policy.timeout_seconds}s".encode(),
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
        comm_future: asyncio.Future = loop.run_in_executor(None, proc.communicate)
        cancel_task = asyncio.create_task(cancel_event.wait())

        done, _ = await asyncio.wait(
            {comm_future, cancel_task},
            timeout=policy.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if cancel_task in done:
            # cancel_inflight() fired: kill process group + return partial output.
            await _kill_proc_group(proc)
            cancel_task.cancel()
            # Read whatever output was captured before the kill.
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
            # Timeout: kill and return with timeout marker.
            cancel_task.cancel()
            await _kill_proc_group(proc)
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
            # Normal completion.
            cancel_task.cancel()
            stdout_b, stderr_b = await comm_future
            return SandboxResult(
                returncode=proc.returncode,
                stdout=stdout_b or b"",
                stderr=stderr_b or b"",
            )
