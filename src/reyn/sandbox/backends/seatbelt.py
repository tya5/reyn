"""SeatbeltBackend — macOS sandbox-exec SBPL wrapper (FP-0017 Component C).

This module is macOS-only. It wraps the `sandbox-exec` binary, which applies a
Sandbox Policy Language (SBPL) profile to restrict filesystem access, network
access, and subprocess spawning for a child process.

**Deprecation notice**: `sandbox-exec` and the SBPL runtime are deprecated
upstream by Apple and are scheduled for removal in macOS 26. On macOS 26+,
`SeatbeltBackend.available()` returns False. The planned successor is
`AppleContainerBackend` (FP-0017 Component E, deferred until macOS 26 ships
stable container APIs).

References:
- FP-0017 Component C: docs/deep-dives/proposals/0017-sandboxed-execution.ja.md
- SBPL reference: Apple TN3137 / sandbox-exec(1) man page
- AppleContainerBackend (deferred): FP-0017 Component E
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from reyn.sandbox.backend import SandboxResult
from reyn.sandbox.policy import SandboxPolicy

_logger = logging.getLogger(__name__)

# SBPL always-allowed system paths required for dynamic library loading and
# basic process bootstrap. Without these, virtually every binary segfaults.
_ALWAYS_READ_SUBPATHS: tuple[str, ...] = (
    "/usr/lib",
    "/System/Library",
    "/usr/bin",
    "/bin",
    "/usr/share",
)


def _sbpl_quote(s: str) -> str:
    """Return an SBPL-safe double-quoted string literal for path *s*.

    SBPL uses Lisp-style string quoting:
    - backslash (\\) is escaped to \\\\
    - double-quote (") is escaped to \\"
    The result is wrapped in double-quotes.
    """
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _build_sbpl_profile(policy: SandboxPolicy) -> str:
    """Generate an SBPL profile string from *policy*.

    The profile starts with a safe ``(deny default)`` base and then adds
    explicit ``(allow ...)`` rules according to the declared policy fields.
    """
    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        "; — system libraries (required for dylib loading) —",
    ]

    # Always-allowed read paths for dylib / process bootstrap.
    system_subpaths = " ".join(
        f"(subpath {_sbpl_quote(p)})" for p in _ALWAYS_READ_SUBPATHS
    )
    lines.append(f"(allow file-read* {system_subpaths})")

    # User-declared read paths.
    if policy.read_paths:
        lines.append("")
        lines.append("; — policy read_paths —")
        for raw in policy.read_paths:
            resolved = str(Path(raw).resolve(strict=False))
            lines.append(f"(allow file-read* (subpath {_sbpl_quote(resolved)}))")

    # User-declared write paths (write implies read in SBPL).
    if policy.write_paths:
        lines.append("")
        lines.append("; — policy write_paths —")
        for raw in policy.write_paths:
            resolved = str(Path(raw).resolve(strict=False))
            lines.append(f"(allow file-read* (subpath {_sbpl_quote(resolved)}))")
            lines.append(f"(allow file-write* (subpath {_sbpl_quote(resolved)}))")

    # Network.
    if policy.network:
        lines.append("")
        lines.append("; — network —")
        lines.append("(allow network*)")

    # Subprocess / fork.
    if policy.allow_subprocess:
        lines.append("")
        lines.append("; — subprocess —")
        lines.append("(allow process-fork)")

    return "\n".join(lines) + "\n"


class SeatbeltBackend:
    """macOS sandbox-exec backend (FP-0017 Component C).

    Generates an SBPL deny-default profile from SandboxPolicy and invokes
    ``sandbox-exec -f <profile> <argv>`` in a subprocess. The profile is
    written to a temporary ``.sb`` file and cleaned up after the subprocess
    returns.

    Availability:
    - Requires macOS (Darwin).
    - Requires ``sandbox-exec`` on PATH.
    - Returns False on macOS 26+ (Apple has removed sandbox-exec).
    """

    name: str = "seatbelt"

    def available(self) -> bool:
        """Return True iff sandbox-exec is usable on this platform."""
        if platform.system() != "Darwin":
            return False
        if shutil.which("sandbox-exec") is None:
            return False
        # sandbox-exec is removed in macOS 26+.
        try:
            ver_str = platform.mac_ver()[0]  # e.g. "14.5.0"
            major = int(ver_str.split(".")[0])
            if major >= 26:
                return False
        except (ValueError, IndexError):
            # Version parsing failed — assume current macOS; return True.
            pass
        return True

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
    ) -> SandboxResult:
        """Execute *argv* under the SBPL policy derived from *policy*."""
        profile_text = _build_sbpl_profile(policy)

        # Build env from passthrough allowlist; fall back PATH if not listed.
        env: dict[str, str] = {}
        for name in policy.env_passthrough:
            if name in os.environ:
                env[name] = os.environ[name]
        if "PATH" not in env and "PATH" in os.environ:
            env["PATH"] = os.environ["PATH"]

        loop = asyncio.get_running_loop()

        def _run_blocking() -> SandboxResult:
            profile_path: str | None = None
            try:
                # Write SBPL to a named temp file (suffix required by sandbox-exec).
                with tempfile.NamedTemporaryFile(
                    suffix=".sb",
                    mode="w",
                    delete=False,
                    encoding="utf-8",
                ) as fh:
                    fh.write(profile_text)
                    profile_path = fh.name

                full_argv = ["sandbox-exec", "-f", profile_path, *argv]
                try:
                    completed = subprocess.run(
                        full_argv,
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
            finally:
                if profile_path is not None:
                    try:
                        os.unlink(profile_path)
                    except OSError:
                        pass

        return await loop.run_in_executor(None, _run_blocking)
