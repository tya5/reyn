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

from reyn.security.sandbox._derivation_cache import cached_derivation
from reyn.security.sandbox._subprocess_io import communicate_capped, kill_process_tree
from reyn.security.sandbox.backend import (
    AxisEnforcement,
    AxisEnforcementDeclaration,
    SandboxResult,
    WrappedCommand,
)
from reyn.security.sandbox.capability import CapabilityDeclaration, CapabilitySupport
from reyn.security.sandbox.policy import (
    POST_KILL_DRAIN_GRACE_SECONDS,
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
        "",
        "; #4932/#4933 (owner ruling, 2026-08-19): a real command that works",
        "; OUTSIDE the sandbox (e.g. `gh auth status`) failed silently inside it",
        "; because nobody had enumerated `security`/`gh`'s one required",
        "; mach-lookup service — a capability #3901's ratified posture (\"the",
        "; sandbox no longer re-decides what the launching shell could already",
        "; do\") already covers in INTENT but SandboxPolicy has no axis for.",
        "; Confirmed by direct measurement (architect, #4932/#4935): the",
        "; `security`/Keychain failure and `gh auth status`'s token-invalid",
        "; failure share this ONE root cause; adding this single, `global-name`-",
        "; scoped grant (not a blanket `(allow mach-lookup)`) fixes both. Owner:",
        "; \"if this makes `gh` work under the default config, go ahead.\" On by",
        "; default (not gated by a policy field) — a future STRICT/opt-in mode",
        "; that wants to close this is a separate, deliberate decision (#4935),",
        "; not something this fix pre-empts.",
        '(allow mach-lookup (global-name "com.apple.SecurityServer"))',
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
    if not policy.deny_subprocess:
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
    # deny."
    #
    # #3901 PR-B ④: read_deny_paths and write_deny_paths are now separate,
    # explicit fields — each denies only its own axis. Before PR-B,
    # read_deny_paths ALSO emitted a file-write* deny as an (undocumented)
    # side-effect (Landlock never replicated that side-effect at all, so the
    # SAME policy meant different things per OS — closed by giving both axes
    # their own real field on both backends). An operator who wants a path
    # denied on BOTH axes now lists it in both fields.
    if policy.read_deny_paths:
        lines.append("")
        lines.append("; — read deny-list (defense-in-depth, deny always wins) —")
        for raw in policy.read_deny_paths:
            resolved = str(expand_policy_path(raw).resolve(strict=False))
            lines.append(f"(deny file-read* (subpath {_sbpl_quote(resolved)}))")

    if policy.write_deny_paths:
        lines.append("")
        lines.append("; — write deny-list (defense-in-depth, deny always wins) —")
        for raw in policy.write_deny_paths:
            resolved = str(expand_policy_path(raw).resolve(strict=False))
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

    # process-fork is gated on policy.deny_subprocess above (#1914, renamed
    # #3901 PR-B ④), so deny_subprocess=True is ENFORCED, not advisory:
    # spawning a child needs fork(); the interpreter is exec'd by
    # sandbox-exec via process-exec* and does not itself need fork to run.
    # Matches the Linux seccomp enforcement.

    return "\n".join(lines) + "\n"


# A dedicated subdirectory of the OS temp dir, not the temp dir's root — this
# keeps the safety check below crisp (one directory to test, not "wherever
# tempfile.mkstemp happens to land") and keeps reyn's cached profiles visually
# distinguishable from other processes' temp files.
_CACHE_SUBDIR_NAME = "reyn-sandbox-profiles"


def _seatbelt_cache_dir() -> Path:
    """Return the directory session-cached SBPL profiles are written under.

    A function, not a module-level constant, so :func:`_profile_is_safe_to_cache`
    below always re-derives the CURRENT value rather than a value captured at
    import time — ``tempfile.gettempdir()`` itself never changes within a
    process, but this keeps the two functions symmetric and re-testable.
    """
    return Path(tempfile.gettempdir()) / _CACHE_SUBDIR_NAME


def _profile_is_safe_to_cache(policy: SandboxPolicy) -> bool:
    """True iff :func:`_seatbelt_cache_dir` falls OUTSIDE every write scope
    *policy* itself grants (#4434's load-bearing precondition, architect
    ruling: caching a profile turns its lifetime from "one call" to "the
    session" — a sandboxed child that could WRITE to the cache path could
    rewrite the profile that governs the NEXT command, i.e. write the key to
    its own cage from inside it).

    Derives the check from *policy.write_paths* itself (via the same
    ``expand_policy_path`` + ``resolve`` every emitted ``(allow file-write*
    (subpath ...))`` rule in :func:`_build_sbpl_profile` uses) rather than a
    literal path comparison — a relocation of either side (the cache
    directory, or an operator's write_paths) is caught by construction,
    where a hardcoded path-string comparison would need to be remembered and
    updated by hand.

    Only the SUBPATH direction is unsafe: a write grant on ``write_paths``
    makes that path and everything BELOW it writable, never anything above
    it, so the cache dir being an *ancestor* of a write_paths entry is fine
    (e.g. cache dir ``/tmp/reyn-sandbox-profiles`` and a write grant on
    ``/tmp/reyn-sandbox-profiles/../workspace`` never overlaps the cache
    dir's own files).
    """
    cache_dir = _seatbelt_cache_dir().resolve(strict=False)
    for raw in policy.write_paths:
        write_scope = expand_policy_path(raw).resolve(strict=False)
        if cache_dir == write_scope or cache_dir.is_relative_to(write_scope):
            return False
    return True


def _cached_profile_path(policy: SandboxPolicy, profile_text: str) -> tuple[str, bool]:
    """Return ``(profile_path, is_cached)`` for *profile_text*.

    Delegates the "derive once per (backend, policy object)" bookkeeping to
    :func:`~reyn.security.sandbox._derivation_cache.cached_derivation`
    (#4434 — shared across every backend that needs this, not Seatbelt-only:
    see that module's docstring for why identity-keyed, not content-keyed).

    When :func:`_profile_is_safe_to_cache` cannot prove the cache directory
    is outside *policy*'s own write scope, the UNSAFE fallback path below
    deliberately bypasses the shared cache entirely rather than caching the
    "don't cache" answer — memoizing that answer under this policy's
    identity would hand a SECOND caller the same path a first caller's
    ``cleanup()`` had already unlinked (each unsafe call gets its own
    private ``tempfile.NamedTemporaryFile``, unlinked on cleanup, byte-for-
    byte the pre-#4434 behaviour). ``is_cached`` tells the caller whether
    the returned path is a shared, session-lifetime file (do NOT unlink it
    in ``cleanup()``) or a private, call-scoped one (DO unlink it).
    """
    if not _profile_is_safe_to_cache(policy):
        with tempfile.NamedTemporaryFile(
            suffix=".sb", mode="w", delete=False, encoding="utf-8",
        ) as fh:
            fh.write(profile_text)
            return fh.name, False

    def _write_cached() -> str:
        cache_dir = _seatbelt_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".sb", dir=str(cache_dir))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(profile_text)
        return path

    path = cached_derivation("seatbelt", policy, _write_cached)
    return path, True


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

    # #4039 (D1/D2): Seatbelt enforces every axis — write via SBPL
    # deny-default + explicit allow rules, both deny-lists via SBPL
    # deny-after-allow (the one real backend that CAN express them, unlike
    # Landlock's allowlist-only LSM constraint), network + subprocess via
    # the generated profile's own deny rules, and both env fields via
    # resolve_passthrough_env (this module's own env resolution call).
    enforced_axes: AxisEnforcementDeclaration = AxisEnforcementDeclaration(
        write_paths=AxisEnforcement.ENFORCES,
        write_deny_paths=AxisEnforcement.ENFORCES,
        read_deny_paths=AxisEnforcement.ENFORCES,
        network=AxisEnforcement.ENFORCES,
        deny_subprocess=AxisEnforcement.ENFORCES,
        env_deny_names=AxisEnforcement.ENFORCES,
        allow_env_names=AxisEnforcement.ENFORCES,
    )

    # #4935: SBPL can express a named-service mach-lookup grant (SUPPORTED)
    # — proven, not assumed, by #4937's own `com.apple.SecurityServer` grant
    # in `_build_sbpl_profile` above actually working through this backend's
    # real `run()` path. See `capability.py`'s own module docstring for the
    # scope of this claim (ONE proven service name, 2 more known-needed but
    # not yet granted — this declaration is "the mechanism exists", not
    # "every named service anyone might need already works") and for the
    # CI-witness gap (this claim has no CI-runnable check — 0 macOS
    # runners — verified once here, by a human, on a real Mac).
    supported_capabilities: CapabilityDeclaration = CapabilityDeclaration(
        ipc_named_service=CapabilitySupport.SUPPORTED,
    )

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
        ``deny_subprocess=True`` was refused, else the reason one was not.
        Cached per process; see ``reyn.security.sandbox.self_test``.

        The second axis is this profile's ``(deny process-fork)`` (#1914) — the
        macOS counterpart of the Linux seccomp gate, and the one an ``(import
        "bsd.sb")`` base grants back unless the explicit deny is emitted, which is
        precisely the kind of silent re-grant #2978 was."""
        from reyn.security.sandbox.self_test import enforcement_self_test  # noqa: PLC0415

        return enforcement_self_test(self)

    def probe_binary(self) -> "list[str] | None":
        """#4364 PR-2: ``/usr/bin/true`` (via the shared lookup — macOS
        ships it at that canonical path on every version, but
        ``shutil.which`` is tried first for a PATH-overridden install)."""
        from reyn.security.sandbox.backend import find_posix_true_binary  # noqa: PLC0415

        return find_posix_true_binary()

    def session_artifact_outside_write_scope(self, policy: SandboxPolicy) -> bool:
        """#4434: delegates to :func:`_profile_is_safe_to_cache` — the ONE
        real implementation of the ``SandboxBackend`` contract (Seatbelt is
        the only backend that caches a filesystem artifact across calls;
        see that Protocol method's own docstring)."""
        return _profile_is_safe_to_cache(policy)

    def wrap_command(self, argv: list[str], policy: SandboxPolicy) -> WrappedCommand:
        """Prepend ``sandbox-exec -f <profile>`` to *argv* for a persistent-process
        launch (e.g. a stdio MCP server, #1344).

        #4434 (stage 1): the SBPL profile is now a SESSION-scoped cache keyed
        on *policy* (via :func:`_cached_profile_path`) rather than a fresh
        temp file every call — an unchanged policy within one process renders
        the identical bytes every time (architect measurement), so repeat
        calls reuse the same on-disk path instead of regenerating + rewriting
        it. Only genuinely per-call state (the OS temp file itself, when the
        cache precondition can't be proven for *policy*'s write scope) still
        gets its own unlink-on-cleanup; a CACHED path is shared across every
        caller using this policy in the process, so ``cleanup()`` here must
        NOT unlink it — a second caller reusing the same policy would then
        launch ``sandbox-exec -f <a path that no longer exists>``. Cached
        files are cleaned up at process exit (OS temp-dir housekeeping),
        matching the profile's new session-scoped lifetime rather than the
        old per-call one. ``env`` is the SAME allowlisted build ``run()``
        uses (#3822) — a caller launching the wrapped argv with this env
        gets the identical env-scoping ``run()``'s callers get."""
        profile_text = _build_sbpl_profile(policy)
        profile_path, is_cached = _cached_profile_path(policy, profile_text)

        def _cleanup() -> None:
            if is_cached:
                return
            try:
                os.unlink(profile_path)
            except OSError:
                pass

        env = resolve_passthrough_env(policy)
        if "PATH" not in env and "PATH" in os.environ:
            env["PATH"] = os.environ["PATH"]

        return WrappedCommand(
            argv=["sandbox-exec", "-f", profile_path, *argv],
            env=env,
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
        # #4204 bucket E: see NoopBackend.run's matching comment — a direct
        # exec (no shell) never resets $PWD the way a real shell would, so
        # the whole parent env's stale value would otherwise leak through
        # unmodified to a child whose actual cwd is `cwd`.
        if cwd:
            env["PWD"] = cwd

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

            # #4271/#4277: inner timeout must exceed the outer asyncio.wait's
            # own timeout below — see container_backend.py's identical
            # comment for the full "who owns the deadline" story.
            comm_future: asyncio.Future = loop.run_in_executor(
                None,
                lambda: communicate_capped(
                    proc, max_bytes=policy.max_output_bytes,
                    timeout=policy.timeout_seconds + POST_KILL_DRAIN_GRACE_SECONDS,
                ),
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
