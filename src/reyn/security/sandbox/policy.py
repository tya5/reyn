"""SandboxPolicy — declarative description of what a sandboxed exec may do.

The policy is data only: declared in skill.md (= FP-0017) and passed by the OS
to a SandboxBackend. P3/P7-aligned — the policy is mechanism-agnostic; backend
selection lives in `reyn.security.sandbox.backend.get_default_backend()`.

Scoping model (#1199 realignment, per-axis):
    write   — tight workspace-allowlist (``write_paths``) = the hard guard.
    network — tight (default off / allowlist) = the exfiltration gate.
    exec    — controlled (``allow_subprocess``).
    read    — **broad-allow by default**. The strict read-allowlist was
              abolished: the network gate (off by default) blocks
              exfiltration, so a broad read surface is safe and avoids the
              system-path enumeration that broke Landlock on Linux. A
              defense-in-depth ``read_deny_paths`` carves out sensitive
              credential locations where the backend can express it.

Fields:
    network: allow outbound network access from the sandboxed process
    read_paths: legacy read-allowlist. Under the broad-read scoping model it
        no longer restricts reads (reads are broad by default); retained for
        backward compatibility and as documentation of intended read targets.
    write_paths: filesystem paths the process may write (write implies read)
    read_deny_paths: sensitive paths to DENY from the broad read surface
        (defense-in-depth). Enforced where the backend can express a
        deny-after-allow rule (Seatbelt / SBPL); NOT enforceable on
        allowlist-only backends (Landlock), which rely on the network gate.
        Defaults to OS-level credential locations; ``~`` is expanded.
    allow_subprocess: whether the process may spawn children
    env_passthrough: env-var names that pass through to the sandboxed process
    timeout_seconds: wall-clock cap (enforced by the backend)
"""
from __future__ import annotations

from dataclasses import dataclass, field

# OS-level sensitive paths denied from the broad read surface by default
# (defense-in-depth). These are universal credential / secret store locations,
# not skill-specific (P7 ok) — the same class as the system-bootstrap paths a
# backend always allows. A policy may override ``read_deny_paths`` to widen or
# narrow this set. Workspace-internal secrets (e.g. a project ``.env``) are
# intentionally NOT in the default — the agent operates inside the workspace,
# so a blanket workspace deny would break legitimate reads; an operator who
# needs it can add the path explicitly.
DEFAULT_SENSITIVE_READ_DENY: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.kube",
    "~/.docker/config.json",
    "~/.netrc",
)


@dataclass
class SandboxPolicy:
    """Declarative sandbox policy. See module docstring for field semantics."""

    network: bool = False
    read_paths: list[str] = field(default_factory=list)
    write_paths: list[str] = field(default_factory=list)
    read_deny_paths: list[str] = field(
        default_factory=lambda: list(DEFAULT_SENSITIVE_READ_DENY)
    )
    allow_subprocess: bool = False
    env_passthrough: list[str] = field(default_factory=list)
    timeout_seconds: int = 60


# ── default sandbox policy resolution (#1339 / sandbox-model completion) ──────
#
# The network default lives in ONE place so the owner can flip it trivially
# (no hardcode scatter). Owner decision 2026-06-05: default ON (the operator,
# not the LLM, owns the policy; an operator who wants isolation sets it off via
# reyn.yaml sandbox.policy). Used by both the chat factories and MCP wrap.
DEFAULT_SANDBOX_NETWORK: bool = True


def resolve_sandbox_policy(
    config_policy: dict | None, *, write_paths: list[str] | None = None
) -> dict:
    """Resolve the effective agent-level sandbox policy as a dict.

    Returns the operator-declared ``reyn.yaml sandbox.policy`` mapping when set;
    otherwise a concrete DEFAULT (never None) so the op_runtime handler always
    applies an operator-or-default policy and the LLM-supplied op fields are
    never used as the sandbox policy (closes #1339). The default = broad-read
    (no read_paths) + the sensitive deny-list + ``network`` from
    :data:`DEFAULT_SANDBOX_NETWORK` + ``write_paths`` tight to the workspace.
    """
    if config_policy is not None:
        return dict(config_policy)
    return {
        "network": DEFAULT_SANDBOX_NETWORK,
        "write_paths": list(write_paths or []),
        "read_deny_paths": list(DEFAULT_SENSITIVE_READ_DENY),
    }
