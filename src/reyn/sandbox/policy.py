"""SandboxPolicy — declarative description of what a sandboxed exec may do.

The policy is data only: declared in skill.md (= FP-0017) and passed by the OS
to a SandboxBackend. P3/P7-aligned — the policy is mechanism-agnostic; backend
selection lives in `reyn.sandbox.backend.get_default_backend()`.

Fields:
    network: allow outbound network access from the sandboxed process
    read_paths: filesystem paths the process may read (glob patterns OK)
    write_paths: filesystem paths the process may write
    allow_subprocess: whether the process may spawn children
    env_passthrough: env-var names that pass through to the sandboxed process
    timeout_seconds: wall-clock cap (enforced by the backend)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SandboxPolicy:
    """Declarative sandbox policy. See module docstring for field semantics."""

    network: bool = False
    read_paths: list[str] = field(default_factory=list)
    write_paths: list[str] = field(default_factory=list)
    allow_subprocess: bool = False
    env_passthrough: list[str] = field(default_factory=list)
    timeout_seconds: int = 60
