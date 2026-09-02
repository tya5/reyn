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
from typing import TYPE_CHECKING, Callable

from ._subprocess_io import communicate_capped, kill_process_tree
from .backend import (
    AxisEnforcement,
    AxisEnforcementDeclaration,
    SandboxResult,
    WrappedCommand,
)
from .capability import CapabilityDeclaration, CapabilitySupport
from .policy import POST_KILL_DRAIN_GRACE_SECONDS, SandboxPolicy, resolve_passthrough_env

if TYPE_CHECKING:
    from reyn.hooks.shell_runner import HookProcessContext

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
    # #3901 PR-B ④: resolve_passthrough_env passes the whole environment
    # minus policy.env_deny_names (compat default, owner ruling B) — no
    # longer a curated union with a standard proxy/CA set.
    env = resolve_passthrough_env(policy)
    if "PATH" not in env and "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
    return env


class NoopBackend:
    """Always-available passthrough backend.

    Honors `policy.timeout_seconds` (wall-clock cap) and `policy.env_deny_names`
    (env-var deny-list, #3901 PR-B ④ renamed). All other policy fields are
    recorded for audit only — NoopBackend does not enforce them.

    #1470: when cancel_event is provided and set, kills the subprocess via
    process-group SIGTERM → SIGKILL and returns SandboxResult(cancelled=True).
    """

    name: str = "noop"

    # #4039 (D1/D2): the founding bug this declaration exists to close —
    # Noop enforced nothing yet unenforced_axes() reported nothing, so a
    # quiet Noop run and a quiet Landlock run were indistinguishable from
    # the audit signal alone. Only env_deny_names/allow_env_names are
    # ENFORCES — both flow through resolve_passthrough_env (this class's
    # own _build_env), the one policy mechanism Noop actually applies.
    enforced_axes: AxisEnforcementDeclaration = AxisEnforcementDeclaration(
        write_paths=AxisEnforcement.DOES_NOT_ENFORCE,
        write_deny_paths=AxisEnforcement.DOES_NOT_ENFORCE,
        read_deny_paths=AxisEnforcement.DOES_NOT_ENFORCE,
        network=AxisEnforcement.DOES_NOT_ENFORCE,
        deny_subprocess=AxisEnforcement.DOES_NOT_ENFORCE,
        env_deny_names=AxisEnforcement.ENFORCES,
        allow_env_names=AxisEnforcement.ENFORCES,
    )

    # #4935 (corrected, architect + lead-coder, post-merge review of #4941):
    # NOT_SUPPORTED. `CapabilitySupport`'s own docstring defines the axis as
    # "whether a backend can EXPRESS one named-capability class" — a
    # mechanism question, not a reachability one. Noop has no grant
    # mechanism at all (it restricts nothing, so it never needed one) —
    # the FIRST version of this declaration wrongly answered a different
    # question ("is a required capability reachable under this backend",
    # true by construction since nothing is denied) than the one the enum
    # actually asks. Getting this wrong here had a real, disclosed,
    # operator-visible consequence: `require_capabilities` +
    # `on_unsupported: error` would have REJECTED Landlock (genuinely
    # enforcing) while ACCEPTING Noop (enforcing nothing) — inverted
    # predictability, the opposite of what strict opt-in is for. This
    # value now rejects Noop under that same combination, the behavior a
    # predictability-first reading of the mechanism actually wants.
    supported_capabilities: CapabilityDeclaration = CapabilityDeclaration(
        ipc_named_service=CapabilitySupport.NOT_SUPPORTED,
    )

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
        `tests/security/test_sandbox_self_test_2983.py`), and CodeAct independently
        refuses to run on a backend named "noop" (`codeact_runner.py`), so the
        exemption grants Noop no enforcement credit anywhere.
        """
        return None

    def probe_binary(self) -> "list[str] | None":
        """#4364 PR-2: always ``None`` — NoopBackend enforces no axis
        (``enforced_axes`` above, all ``DOES_NOT_ENFORCE``), so a
        differential launch probe here would answer a question this
        backend has no sandbox to differentiate on: whatever argv[0]
        resolves to under a plain, unsandboxed exec, so would any other
        binary. Same exemption class as ``self_test`` above — Noop
        genuinely has nothing to probe, not a gap in this method."""
        return None

    def session_artifact_outside_write_scope(self, policy: SandboxPolicy) -> bool:
        """Trivially True (#4434): ``wrap_command`` below returns *argv*
        UNCHANGED — no profile, no wrapper argv, no on-disk artifact of any
        kind — so there is nothing a sandboxed child could rewrite. Still
        bears the contract (owner correction, #4434): a future NoopBackend
        change that starts writing something would need to answer this
        honestly, not inherit a silent pass by omission."""
        return True

    def wrap_command(self, argv: list[str], policy: SandboxPolicy) -> WrappedCommand:
        """Passthrough: argv is returned UNCHANGED — no enforcement — but the
        call still went THROUGH the sandbox abstraction (the owner-acceptable
        no-isolation case, #2620), as opposed to a caller that never consulted
        any backend at all. ``env`` is still the allowlisted build (#3822):
        no OS isolation is applied on this backend, but the ENV-scoping
        contract (never hand a model-authored subprocess the full parent
        environment merely because the sandbox backend is Noop) is unrelated
        to OS enforcement and stays in force."""
        _warn_once()
        return WrappedCommand(argv=list(argv), env=_build_env(policy), cleanup=None)

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        cancel_event: asyncio.Event | None = None,
        hook_process_context: "HookProcessContext | None" = None,
        sink: "Callable[[int, bytes], None] | None" = None,
    ) -> SandboxResult:
        _warn_once()

        env = _build_env(policy)
        # #4204 bucket E: a real shell resets $PWD to its own cwd at startup;
        # a direct exec (no shell in between) does not — the whole parent
        # env passes through (resolve_passthrough_env, #3901 PR-B ④)
        # unmodified, so a stale $PWD (reyn's own launch directory) would
        # otherwise reach a child whose ACTUAL cwd is `cwd` (e.g. a
        # subdirectory-launch's real project root, #4204 condition ①). A
        # tool that trusts $PWD instead of calling getcwd() would see the
        # wrong directory with no way to detect it.
        if cwd:
            env["PWD"] = cwd
        # #5084 ④: this is a real host process (no container boundary), so
        # every one of hook_process_context's 3 keys applies unchanged —
        # see SandboxBackend.run's own docstring for the closed-envelope
        # contract this merges verbatim, never a caller-chosen env.
        if hook_process_context is not None:
            env.update(hook_process_context.as_env())

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
                        sink=sink,
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
        # #4271/#4277: inner timeout must exceed the outer asyncio.wait's own
        # timeout below (see container_backend.py's identical comment for
        # the full "who owns the deadline" story) — same value let
        # subprocess.TimeoutExpired escape uncaught through this function's
        # plain `await comm_future` normal-completion branch.
        comm_future: asyncio.Future = loop.run_in_executor(
            None,
            lambda: communicate_capped(
                proc, max_bytes=policy.max_output_bytes,
                timeout=policy.timeout_seconds + POST_KILL_DRAIN_GRACE_SECONDS,
                sink=sink,
            ),
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
