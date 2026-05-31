"""SeatbeltBackend — macOS sandbox-exec SBPL wrapper (FP-0017 Component C).

This module is macOS-only. It wraps the `sandbox-exec` binary, which applies a
Sandbox Policy Language (SBPL) profile to restrict filesystem access, network
access, and subprocess spawning for a child process.

**Deprecation notice**: `sandbox-exec` and the SBPL runtime are deprecated
upstream by Apple. As of macOS 26.3 the binary is still shipped at
`/usr/bin/sandbox-exec` and functional; `available()` keys off binary
presence rather than macOS major version, so if a future macOS truly
removes the binary the backend will naturally report unavailable and the
factory will fall through to `AppleContainerBackend` (FP-0017 Component E,
deferred until macOS ships stable container APIs).

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
    # dyld cache lives under /private/var/db/dyld on modern macOS; without
    # read access the dynamic linker can't map shared cache and binaries
    # abort at libc init.
    "/private/var/db/dyld",
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
        "; — base BSD syscall baseline (Apple-provided, /usr/share/sandbox) —",
        "; bsd.sb supplies mach-lookup, sysctl-read, signal, ipc-posix-shm,",
        "; iokit-open subset, etc. — the minimum required to actually run a",
        "; binary under (deny default). Without it, even /bin/echo aborts at",
        "; libc init (SIGABRT) on macOS 26+.",
        '(import "bsd.sb")',
        "",
        "; — system libraries (dyld cache + framework load paths) —",
    ]

    # Always-allowed read paths for dylib / process bootstrap.
    system_subpaths = " ".join(
        f"(subpath {_sbpl_quote(p)})" for p in _ALWAYS_READ_SUBPATHS
    )
    lines.append(f"(allow file-read* {system_subpaths})")

    # Always-allowed process-exec: without this, sandbox-exec cannot even
    # execvp() the target binary under (deny default) (macOS 26+ is strict).
    # The filesystem restrictions above still bound what the exec'd process
    # can read/write/network; we just don't gate the exec syscall itself.
    # process-fork is similarly needed by virtually every interpreter / runtime
    # bootstrap (e.g. CRT init); policy.allow_subprocess remains advisory.
    lines.append("(allow process-exec*)")
    lines.append("(allow process-fork)")

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

    # Note: process-fork is allowed unconditionally above. policy.allow_subprocess
    # is currently advisory under Seatbelt — distinguishing "the invoked binary
    # may fork (interpreter bootstrap)" from "and may spawn arbitrary children"
    # requires per-pid rules SBPL doesn't cleanly express. Recorded in P6
    # events for audit.

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

    Note: the FP-0017 doc anticipated Apple removing sandbox-exec in macOS 26
    in favor of Apple Containers. As of macOS 26.3, sandbox-exec is still
    shipped at /usr/bin/sandbox-exec (deprecated upstream but functional),
    so we trust the presence of the binary rather than gating on macOS
    major version. If a future macOS truly removes the binary, ``shutil.which``
    will return None and ``available()`` will naturally fall back to False
    (then AppleContainerBackend / FP-0017 Component E takes over).
    """

    name: str = "seatbelt"

    def available(self) -> bool:
        """Return True iff sandbox-exec is usable on this platform."""
        if platform.system() != "Darwin":
            return False
        if shutil.which("sandbox-exec") is None:
            return False
        return True

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Execute *argv* under the SBPL policy derived from *policy*.

        ``cwd`` (= the run's ``workspace.base_dir``) is the working directory the
        sandboxed child inherits, so repo-relative ``git`` / ``pytest`` resolve
        correctly. The SBPL profile still bounds what that child may read/write.
        """
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
            finally:
                if profile_path is not None:
                    try:
                        os.unlink(profile_path)
                    except OSError:
                        pass

        return await loop.run_in_executor(None, _run_blocking)
