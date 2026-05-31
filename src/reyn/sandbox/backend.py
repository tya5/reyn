"""SandboxBackend Protocol + SandboxResult — mechanism abstraction (FP-0017).

The Protocol decouples op handlers from the enforcement mechanism. Concrete
backends (NoopBackend today; SeatbeltBackend / LandlockBackend in future
waves) implement `available()` for platform detection and `run()` for actual
execution under the declared policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .policy import SandboxPolicy


@dataclass
class SandboxResult:
    """Result of a single sandboxed_exec invocation.

    `truncated` indicates that stdout/stderr were capped by the backend.
    `returncode` is -1 if the process was killed (timeout / signal).
    """

    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False


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

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
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
        """
        ...
