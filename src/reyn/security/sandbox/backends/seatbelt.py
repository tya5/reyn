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
import signal
import subprocess
import tempfile
from pathlib import Path

from reyn.security.sandbox._subprocess_io import communicate_capped, kill_process_tree
from reyn.security.sandbox.backend import SandboxResult, WrappedCommand
from reyn.security.sandbox.policy import (
    SandboxPolicy,
    expand_policy_path,
    resolve_passthrough_env,
)

_logger = logging.getLogger(__name__)

# #1199 realignment: the broad ``(allow file-read*)`` rule below subsumes the
# old explicit system-path allowlist (/usr/lib, /System/Library, dyld cache,
# …) that dynamic-library loading and process bootstrap required. With a broad
# read surface there is no system-path enumeration to maintain.


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
    ]

    # process-exec* is always allowed: without it sandbox-exec cannot even
    # execvp() the target binary under (deny default) (macOS 26+ is strict).
    # This permits only the INITIAL exec of the target, NOT child spawning —
    # spawning a child additionally needs process-fork, gated below.
    lines.append("")
    lines.append("(allow process-exec*)")
    # process-fork gates child spawning (#1914). IMPORTANT: the (import "bsd.sb")
    # base above GRANTS process-fork, so merely omitting our own (allow ...) is
    # NOT sufficient — emit an explicit (deny process-fork) (SBPL is
    # last-match-wins) to override the base grant when subprocess is disallowed.
    # A child spawn (subprocess / os.posix_spawn / os.system / multiprocessing /
    # shell pipeline) needs fork() and is then denied, while the interpreter
    # itself, threading, and a single exec-replacement still run (those need only
    # process-exec*). Linux-parity with the seccomp gate; verified via sandbox-exec
    # (py3.9/3.12 + sh pipeline). The prior "fork needed for runtime bootstrap"
    # rationale was incorrect.
    if policy.allow_subprocess:
        lines.append("(allow process-fork)")
    else:
        lines.append("(deny process-fork)")

    # #1199 realignment — broad read surface. The strict read-allowlist was
    # abolished: reads are broad by default (this subsumes the old system-path
    # bootstrap allowlist AND policy.read_paths). Safety comes from the network
    # gate (off unless policy.network): a process may read widely but cannot
    # exfiltrate.
    lines.append("")
    lines.append("; — broad read (the network gate is the exfiltration guard) —")
    lines.append("(allow file-read*)")

    # User-declared write paths. write implies read, so each grant re-allows
    # both file-read* and file-write* for its subpath.
    if policy.write_paths:
        lines.append("")
        lines.append("; — policy write_paths —")
        for raw in policy.write_paths:
            # expand_policy_path: ``~`` MUST expand here exactly as it does for
            # read_deny_paths below — without it the grant lands on the literal
            # ``<cwd>/~/...`` and the write stays denied (#2976).
            resolved = str(expand_policy_path(raw).resolve(strict=False))
            lines.append(f"(allow file-read* (subpath {_sbpl_quote(resolved)}))")
            lines.append(f"(allow file-write* (subpath {_sbpl_quote(resolved)}))")

    # Defense-in-depth: deny sensitive paths. SBPL is last-match-wins, so these
    # (deny ...) rules are emitted AFTER the broad read allow AND after the
    # write_paths grants above — so an operator's deny of a credential path
    # ALWAYS wins over a broad write grant that would otherwise engulf it
    # (#2978). This is the owner rule: "a deny that loses to an allow is not a
    # deny." Previously the write re-allow was placed after the deny-list, so
    # ``write_paths=[$HOME]`` silently nullified the ``~/.ssh`` etc. denies —
    # ``~/.ssh`` became both readable AND writable. Both axes are denied here
    # (not just file-read*): a write grant makes an engulfed credential path
    # WRITABLE too, so denying only reads would leave ``touch ~/.ssh/x``
    # succeeding. An operator who genuinely needs to write under a denied prefix
    # narrows ``read_deny_paths`` explicitly; the op handler emits a
    # ``sandbox_policy_narrowed`` audit-event whenever a deny wins here, so the
    # narrowing is observable rather than silent.
    if policy.read_deny_paths:
        lines.append("")
        lines.append("; — sensitive deny-list (defense-in-depth, deny always wins) —")
        for raw in policy.read_deny_paths:
            resolved = str(expand_policy_path(raw).resolve(strict=False))
            lines.append(f"(deny file-read* (subpath {_sbpl_quote(resolved)}))")
            lines.append(f"(deny file-write* (subpath {_sbpl_quote(resolved)}))")

    # Always-allowed loopback bind (#3060), independent of `policy.network`.
    # `network-bind` scoped to `localhost:*` lets a process claim a LOCAL
    # address (IPv4 127.0.0.1 / IPv6 ::1) on any port, but grants neither
    # `network-outbound` (dialing a remote peer) nor `network-inbound`
    # (accepting one) nor an unscoped `network-bind` to a non-loopback
    # address — so a network-off sandbox still cannot reach the network.
    # This closes a false-positive class measured live: urllib3's
    # import-time IPv6-support probe (`urllib3/util/connection.py:137`,
    # reached transitively via fastmcp -> requests -> urllib3) calls
    # `socket.socket()` then `sock.bind(("::1", 0))` — a loopback bind,
    # never a `connect()` — and used to abort the sandboxed process with a
    # permission error under `network: false` even though it never touches
    # the network. Root-caused and confirmed benign before this fix (see
    # issue #3060). Mirrors seccomp's `_NETWORK_ALWAYS_ALLOWED` (socket +
    # bind) — `network-bind` is Seatbelt's `bind(2)` equivalent.
    lines.append("")
    lines.append("; — always-allowed loopback bind (the network gate stays the egress guard) —")
    lines.append('(allow network-bind (local ip "localhost:*"))')

    # Network.
    if policy.network:
        lines.append("")
        lines.append("; — network —")
        lines.append("(allow network*)")

    # process-fork is gated on policy.allow_subprocess above (#1914), so
    # allow_subprocess=False is ENFORCED, not advisory: spawning a child needs
    # fork(); the interpreter is exec'd by sandbox-exec via process-exec* and
    # does not itself need fork to run. Matches the Linux seccomp enforcement.

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
        """Return True iff the sandbox-exec mechanism is PRESENT on this platform.

        Presence only — Darwin + the binary on PATH. Whether the profile this
        backend generates actually denies anything is ``self_test()``'s question
        (#2983): #2978 was a live Seatbelt whose deny-list was silently
        overridden, and this method reported True throughout.
        """
        if platform.system() != "Darwin":
            return False
        if shutil.which("sandbox-exec") is None:
            return False
        return True

    def self_test(self) -> str | None:
        """Witness real denies through ``sandbox-exec`` (#2983): None when a write
        outside ``write_paths`` was refused AND a spawn under
        ``allow_subprocess=False`` was refused, else the reason one was not.
        Cached per process; see ``reyn.security.sandbox.self_test``.

        The second axis is this profile's ``(deny process-fork)`` (#1914) — the
        macOS counterpart of the Linux seccomp gate, and the one an ``(import
        "bsd.sb")`` base grants back unless the explicit deny is emitted, which is
        precisely the kind of silent re-grant #2978 was."""
        from reyn.security.sandbox.self_test import enforcement_self_test  # noqa: PLC0415

        return enforcement_self_test(self)

    def wrap_command(self, argv: list[str], policy: SandboxPolicy) -> WrappedCommand:
        """Prepend ``sandbox-exec -f <profile>`` to *argv* for a persistent-process
        launch (e.g. a stdio MCP server, #1344). The SBPL profile is written to a
        temp ``.sb`` file; the returned ``cleanup`` unlinks it — the caller invokes
        it once the wrapped subprocess is torn down (mirrors ``run()``'s own
        temp-profile lifecycle, above)."""
        profile_text = _build_sbpl_profile(policy)
        with tempfile.NamedTemporaryFile(
            suffix=".sb", mode="w", delete=False, encoding="utf-8",
        ) as fh:
            fh.write(profile_text)
            profile_path = fh.name

        def _cleanup() -> None:
            try:
                os.unlink(profile_path)
            except OSError:
                pass

        return WrappedCommand(
            argv=["sandbox-exec", "-f", profile_path, *argv],
            cleanup=_cleanup,
        )

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SandboxResult:
        """Execute *argv* under the SBPL policy derived from *policy*.

        ``cwd`` (= the run's ``workspace.base_dir``) is the working directory the
        sandboxed child inherits, so repo-relative ``git`` / ``pytest`` resolve
        correctly. The SBPL profile still bounds what that child may read/write.

        ``cancel_event``: when provided and set, kills the sandbox-exec wrapper
        process group (SIGTERM → SIGKILL) and returns SandboxResult(cancelled=True).
        """
        profile_text = _build_sbpl_profile(policy)

        # Build env from passthrough allowlist ∪ the standard proxy/CA env
        # (#3075); fall back PATH if not listed.
        env = resolve_passthrough_env(policy)
        if "PATH" not in env and "PATH" in os.environ:
            env["PATH"] = os.environ["PATH"]

        loop = asyncio.get_running_loop()

        # Write SBPL profile to a temp file (shared between blocking and cancel paths).
        profile_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".sb", mode="w", delete=False, encoding="utf-8",
            ) as fh:
                fh.write(profile_text)
                profile_path = fh.name
        except OSError as exc:
            return SandboxResult(returncode=-1, stdout=b"", stderr=str(exc).encode())

        full_argv = ["sandbox-exec", "-f", profile_path, *argv]

        try:
            if cancel_event is None:
                # No cancel support: original blocking path (byte-identical).
                def _run_blocking() -> SandboxResult:
                    try:
                        proc = subprocess.Popen(
                            full_argv,
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
                    full_argv,
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
        finally:
            if profile_path is not None:
                try:
                    os.unlink(profile_path)
                except OSError:
                    pass
