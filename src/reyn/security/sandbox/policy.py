"""SandboxPolicy — declarative description of what a sandboxed exec may do.

The policy is data only: declared in the agent profile (= FP-0017) and passed by the OS
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
    write_paths: filesystem paths the process may write (write implies read).
        ``~`` is expanded (see :func:`expand_policy_path`).
    read_deny_paths: sensitive paths to DENY from the broad read surface
        (defense-in-depth). Enforced where the backend can express a
        deny-after-allow rule (Seatbelt / SBPL); NOT enforceable on
        allowlist-only backends (Landlock), which rely on the network gate.
        Defaults to OS-level credential locations; ``~`` is expanded.
    allow_subprocess: whether the process may spawn children
    env_passthrough: env-var names that pass through to the sandboxed process
    timeout_seconds: wall-clock cap (enforced by the backend)
    max_output_bytes: per-stream cap (bytes) on captured stdout/stderr — output
        beyond it is drained-and-discarded (the ``truncated`` flag is set) so a
        flooding child cannot exhaust host memory. Default 10 MiB; overridable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ._subprocess_io import MAX_SUBPROCESS_OUTPUT_BYTES


def expand_policy_path(raw: str) -> Path:
    """Expand a leading ``~`` in a policy path — the SHARED contract every
    backend must apply to every path field it enforces (#2976).

    Policy paths are operator-authored (``reyn.yaml sandbox.policy``, an MCP
    server's ``write_paths``), so ``~/.npm`` is the natural way to write a
    home-relative path and MUST mean ``$HOME/.npm``.

    This exists because the expansion was applied to ``read_deny_paths`` but
    NOT to ``write_paths``: ``Path("~/.npm").resolve()`` silently yields
    ``<cwd>/~/.npm`` — a literal ``~`` directory that does not exist. The grant
    was therefore emitted for a path the process never touches, so the write
    stayed denied while the policy object *looked* correct. Nothing failed
    loudly; the only symptom was an opaque ``Operation not permitted`` from a
    path the operator had explicitly allowed.

    Deliberately does NOT call ``resolve()``: symlink resolution is a separate,
    backend-specific decision (Seatbelt resolves for SBPL subpath matching;
    Landlock hands the path to the kernel as-is). Callers add it where they
    need it, so this helper stays no more opinionated than its least
    opinionated caller.
    """
    return Path(raw).expanduser()

# OS-level sensitive paths denied from the broad read surface by default
# (defense-in-depth). These are universal credential / secret store locations,
# not domain-specific (P7 ok) — the same class as the system-bootstrap paths a
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


def resolve_passthrough_env(policy: "SandboxPolicy") -> dict[str, str]:
    """Build the env dict every sandbox backend passes to a spawned child
    (#3075 fix 5 — the shared chokepoint all three backends call).

    ``policy.env_passthrough`` ∪ the standard proxy/CA env
    (:data:`reyn._network.STANDARD_NETWORK_ENV_NAMES`) — the sandbox forwards
    the standard set to EVERY sandboxed child by default, generalising the
    git-clone-specific forwarding that used to live only in
    ``skill_install.py`` (#3075's sharpest symptom: git-clone conformed,
    its sibling uvx/npx subprocess did not). This is additive only — an
    operator-declared ``env_passthrough`` entry is still honoured, and no
    secret-bearing var is ever added here (the standard set is a curated,
    known-non-secret allowlist: proxy URLs + CA bundle *paths*, not
    credentials — the CA bundle *file* was already broad-read-floor
    readable; only the env var pointing at it was missing before #3075).

    PATH fallback is applied by each backend after calling this (preserves the
    existing "PATH always available" behaviour independent of this set).
    """
    from reyn._network import STANDARD_NETWORK_ENV_NAMES

    names = set(policy.env_passthrough) | set(STANDARD_NETWORK_ENV_NAMES)
    return {name: os.environ[name] for name in names if name in os.environ}


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
    max_output_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES


def deny_narrowed_write_grants(policy: SandboxPolicy) -> list[tuple[str, str]]:
    """Return ``(write_path, deny_path)`` pairs where a ``read_deny_paths`` entry
    overlaps a ``write_paths`` grant — i.e. where the deny-always-wins rule
    (#2978) actually NARROWS a grant the operator/caller declared.

    Pure function of the policy (no I/O, no events) so it is trivially testable
    and can be called from any layer that has an events sink. The op handler
    uses it to emit a ``sandbox_policy_narrowed`` audit-event so a narrowing is
    never silent — the owner requirement that a deny winning over a write grant
    is observable, not a silent drop.

    Overlap = either path contains the other, matching the SBPL ``subpath``
    semantics the Seatbelt backend enforces (a deny on ``~/.ssh`` narrows a
    write grant on ``~``; a deny on ``~/.ssh`` also fully nullifies an explicit
    write grant on ``~/.ssh/x`` — both are reported so the operator can widen
    ``read_deny_paths`` if the write was intended). Paths are ``~``-expanded and
    resolved to match what the backend compares.
    """
    writes = [
        (raw, expand_policy_path(raw).resolve(strict=False))
        for raw in policy.write_paths
    ]
    denies = [
        (raw, expand_policy_path(raw).resolve(strict=False))
        for raw in policy.read_deny_paths
    ]
    narrowed: list[tuple[str, str]] = []
    for w_raw, w in writes:
        for d_raw, d in denies:
            if w == d or w.is_relative_to(d) or d.is_relative_to(w):
                narrowed.append((w_raw, d_raw))
    return narrowed


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

    The concrete DEFAULT is a **floor** (never None) so the op_runtime handler
    always applies an operator-or-default policy and the LLM-supplied op fields
    are never used as the sandbox policy (closes #1339). The floor = broad-read
    (no read_paths) + the sensitive deny-list + ``network`` from
    :data:`DEFAULT_SANDBOX_NETWORK` + ``write_paths`` tight to the workspace
    (the caller-supplied ``write_paths`` = "this op needs this directory", a
    value the operator cannot know).

    An operator-declared ``reyn.yaml sandbox.policy`` mapping is **merged onto
    the floor**, not substituted wholesale (#2964). Only the fields the operator
    actually wrote override the floor; fields they omitted keep the floor value
    — so writing ``allow_subprocess: false`` alone no longer silently drops the
    caller's ``write_paths`` (workspace write access). This is the owner design
    principle: *the default is the floor an operator ADDS to; only an explicit
    write is the operator's expressed will.*

    "Wrote it" is expressed by dict-key presence: ``write_paths: []`` is an
    explicit empty grant (respected — the caller's write_paths are overridden by
    the operator's deliberate empty list), whereas OMITTING ``write_paths``
    keeps the floor's caller-supplied value. dict semantics make the
    "explicit-empty vs omitted" distinction the whole fix hinges on directly
    representable — no separate sentinel is needed.
    """
    floor: dict = {
        "network": DEFAULT_SANDBOX_NETWORK,
        "write_paths": list(write_paths or []),
        "read_deny_paths": list(DEFAULT_SENSITIVE_READ_DENY),
    }
    if config_policy is not None:
        floor.update(config_policy)
    return floor
