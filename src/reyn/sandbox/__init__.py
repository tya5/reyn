"""Sandboxed execution primitives (FP-0017).

Exports:
    SandboxPolicy   — declarative policy (what is allowed)
    SandboxBackend  — Protocol for backends (how it's enforced)
    SandboxResult   — return type of `backend.run()`
    NoopBackend     — default fallback (no isolation enforced)
    get_default_backend() — factory; returns NoopBackend today

Future waves add SeatbeltBackend (macOS) and LandlockBackend (Linux).
"""
from __future__ import annotations

from .backend import SandboxBackend, SandboxResult
from .noop_backend import NoopBackend
from .policy import SandboxPolicy


def get_default_backend() -> SandboxBackend:
    """Return the default backend for this platform.

    Today this always returns NoopBackend. The platform-detection logic
    (= SeatbeltBackend on macOS < 26, LandlockBackend on Linux 5.13+) will
    land in FP-0017 Components B and C without touching callers.
    """
    return NoopBackend()


__all__ = [
    "SandboxPolicy",
    "SandboxBackend",
    "SandboxResult",
    "NoopBackend",
    "get_default_backend",
]
