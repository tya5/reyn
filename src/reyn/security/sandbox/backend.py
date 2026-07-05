"""SandboxBackend Protocol + SandboxResult — mechanism abstraction (FP-0017).

The Protocol decouples op handlers from the enforcement mechanism. Concrete
backends (NoopBackend today; SeatbeltBackend / LandlockBackend in future
waves) implement `available()` for platform detection and `run()` for actual
execution under the declared policy.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .policy import SandboxPolicy


@dataclass
class WrappedCommand:
    """Result of ``SandboxBackend.wrap_command()`` — a command-level sandbox wrap.

    Command-level wrapping (as opposed to the one-shot ``run()``) is the seam
    for a PERSISTENT subprocess launch that the backend does not itself spawn
    (e.g. a stdio MCP server held open by the caller's transport) — the wrap
    prepends whatever the backend needs (a sandbox-exec invocation, a re-exec
    shim, ...) and hands the full argv back for the caller to Popen/exec.

    ``argv`` is the full wrapped argv (wrapper prefix + the original command),
    ready to launch directly. ``cleanup``, when set, releases a wrap-owned
    resource (e.g. Seatbelt's temp ``.sb`` profile file) — the caller MUST
    invoke it once the wrapped subprocess is torn down. ``None`` means the
    wrap owns no such resource.
    """

    argv: list[str]
    cleanup: "Callable[[], None] | None" = None


@dataclass
class SandboxResult:
    """Result of a single sandboxed_exec invocation.

    `truncated` indicates that stdout/stderr were capped by the backend.
    `returncode` is -1 if the process was killed (timeout / signal).
    `cancelled` is True when the run was terminated by cancel_inflight() (#1470).
    """

    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    cancelled: bool = False


@runtime_checkable
class SandboxBackend(Protocol):
    """Sandbox backend protocol.

    Implementations declare a `name` attribute (= "noop" / "seatbelt" /
    "landlock" / ...), report platform availability via `available()`, and
    run a command under the supplied policy via `run()`.
    """

    name: str

    def available(self) -> bool:
        """Return True if this backend can be used on the current platform."""
        ...

    def wrap_command(self, argv: list[str], policy: SandboxPolicy) -> WrappedCommand:
        """Return a command-level sandbox wrap of *argv* for a persistent-process
        launch (e.g. a stdio MCP server) that cannot go through the one-shot
        ``run()``. Every backend implements this uniformly so NO agent-reachable
        command-level launch ever bypasses the abstraction:

        - Seatbelt: prepends ``sandbox-exec -f <profile>`` (a generated SBPL
          profile written to a temp file; the returned ``cleanup`` unlinks it).
        - Landlock: prepends the ``landlock_exec`` re-exec shim argv.
        - NoopBackend: returns *argv* UNCHANGED — passthrough, but the call
          still went THROUGH this method (the owner-acceptable no-enforcement
          case, as opposed to a raw bypass that never consulted the backend).

        Synchronous and side-effect-light (may perform local I/O such as
        writing a temp profile file) — it does not itself spawn the wrapped
        process; the caller owns that.
        """
        ...

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> SandboxResult:
        """Execute argv under the given policy and return the result.

        ``cwd`` is the working directory the command runs in. The OS passes the
        run's ``workspace.base_dir`` (= parity with the legacy ``shell`` op,
        FP-0008 PR-I) so ``git`` / ``pytest`` resolve against the repo root even
        under concurrent benchmark runs. ``None`` = inherit the parent process
        cwd. A workspace-coupled backend (e.g. a container backend whose repo
        lives at an in-container path) may ignore this host-side ``cwd`` and use
        its own baked working directory — same asymmetry as policy enforcement,
        which such a backend also scopes to the fidelity boundary.

        ``cancel_event``: when provided and set, the backend kills the running
        subprocess (SIGTERM → SIGKILL grace) and returns a SandboxResult with
        ``cancelled=True``. None = no cancel-awareness (#1470).
        """
        ...
