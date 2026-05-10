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
    ) -> SandboxResult:
        """Execute argv under the given policy and return the result."""
        ...
