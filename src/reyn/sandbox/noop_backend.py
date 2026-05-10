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


class NoopBackend:
    """Always-available passthrough backend.

    Honors `policy.timeout_seconds` (wall-clock cap) and `policy.env_passthrough`
    (env-var allowlist). All other policy fields are recorded for audit only —
    NoopBackend does not enforce them.
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
    ) -> SandboxResult:
        _warn_once()

        # Build env from the passthrough allowlist. Empty list = empty env.
        import os as _os
        env: dict[str, str] = {}
        for name in policy.env_passthrough:
            if name in _os.environ:
                env[name] = _os.environ[name]
        # PATH is required for argv[0] to resolve. If the caller did not
        # include PATH in env_passthrough, fall back to the current PATH so
        # commands like "echo" still resolve. NoopBackend does not enforce.
        if "PATH" not in env and "PATH" in _os.environ:
            env["PATH"] = _os.environ["PATH"]

        loop = asyncio.get_running_loop()

        def _run_blocking() -> SandboxResult:
            try:
                completed = subprocess.run(
                    argv,
                    input=stdin,
                    capture_output=True,
                    env=env,
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
