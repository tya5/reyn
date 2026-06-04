"""Effective-permission model — #1199 S3.1 conjunctive-∩ invariant (S3.1a).

The OS-level permission rule (#1115 / #1199 S3.1): a capability is permitted iff
EVERY permission layer permits it — **effective = ⋂ layers, restrict-only,
grant-back forbidden**. No layer's deny can be re-granted by another layer; ∩ can
only narrow.

Four inputs, three ⋂ layers:
- **agent** (`PermissionDecl` + the default zone): the GRANT layer. Its allow-set
  is the default zone (layer-0 baseline) ∪ the skill's explicit declarations. The
  zone is folded in here as the baseline — NOT a separate ∩ restrictor (a
  separate zone restrictor would cancel the decl grants that intentionally extend
  beyond the zone; the byte-identical requirement forces zone-as-baseline).
- **sandbox** (`SandboxPolicy`): runtime caps (paths / network / subprocess / env).
- **profile** (`AgentProfile`): agent-level allowlists (skills / mcp).

Per #1199 design call (issuecomment-4620567488): Q2 = per-VALUE membership
conjunction (no materialized intersected sets — `allows(axis, value) = ∀L:
L.allows(axis, value)`; path scope handled inside each layer's match). Q3 =
compute per op-context (SandboxPolicy is phase-variable), so build an
`EffectivePermission` from the live layers at gate time and memoize on the
context, not in any resolver `__init__`.

**S3.1a is the model + projections only — UNWIRED (byte-identical).** The live
`PermissionResolver` gates are unchanged; S3.1b switches them to read
`EffectivePermission.allows`. A layer that does not constrain an axis returns
``True`` for it (⊤ — it never narrows the ∩).
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from reyn.permissions.permissions import (
    _decl_covers_path,
    _in_default_read_zone,
    _in_default_write_zone,
)

if TYPE_CHECKING:
    from reyn.chat.profile import AgentProfile
    from reyn.permissions.permissions import PermissionDecl
    from reyn.sandbox.policy import SandboxPolicy


class CapabilityAxis(Enum):
    """The canonical capability axes (#1199 Q1: 9 axes; network at host
    granularity — scheme/port is a deferred follow-up). Every permission layer
    projects onto these, so the ⋂ is computed on one vocabulary."""

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    NETWORK_HOST = "network_host"
    SUBPROCESS = "subprocess"
    MCP = "mcp"
    SKILL = "skill"
    SECRET_WRITE = "secret_write"
    PYTHON = "python"
    ENV = "env"


class LayerView(Protocol):
    """One permission layer's projection. ``allows(axis, value)`` answers, for a
    concrete request value (a path / host / name / (module, function) / env var),
    whether THIS layer permits it. A layer that does not constrain ``axis``
    returns ``True`` (⊤) so it never narrows the conjunction."""

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:  # pragma: no cover
        ...


class AgentLayer:
    """The GRANT layer: the skill's ``PermissionDecl`` over the default-zone
    baseline. File axes allow the default zone ∪ the declared paths (faithful to
    ``require_file_read`` / ``require_file_write`` — reuses the same helpers).
    Startup-approval grants are runtime state, not part of this static
    projection (S3.1b decides whether to fold them in)."""

    def __init__(self, decl: "PermissionDecl") -> None:
        self._decl = decl

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        d = self._decl
        if axis is CapabilityAxis.FILE_READ:
            return _in_default_read_zone(str(value)) or _decl_covers_path(
                d.file_read, str(value)
            )
        if axis is CapabilityAxis.FILE_WRITE:
            return _in_default_write_zone(str(value)) or _decl_covers_path(
                d.file_write, str(value)
            )
        if axis is CapabilityAxis.NETWORK_HOST:
            return any(e.get("host") == value for e in d.http_get)
        if axis is CapabilityAxis.SUBPROCESS:
            return bool(d.shell)
        if axis is CapabilityAxis.MCP:
            # decl grant: the per-skill mcp list (allowed_mcp is the additional
            # per-agent allowlist, projected on the profile layer's MCP axis).
            return value in d.mcp
        if axis is CapabilityAxis.SECRET_WRITE:
            return value in d.secret_write
        if axis is CapabilityAxis.PYTHON:
            # value = (module, function)
            return any(
                (p.module, p.function) == tuple(value) for p in d.python
            )
        # SKILL / ENV: the decl does not constrain → ⊤.
        return True


class SandboxLayer:
    """The RESTRICT layer: ``SandboxPolicy`` runtime caps. An empty path/host list
    means the policy declares no restriction on that axis (⊤) — restrict-only:
    a policy narrows only by listing. ``network``/``allow_subprocess`` are the
    degenerate 2-element lattice (False = ⊥ denies the whole axis)."""

    def __init__(self, policy: "SandboxPolicy | None") -> None:
        self._policy = policy

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        p = self._policy
        if p is None:
            return True  # no sandbox layer → unrestricted
        if axis is CapabilityAxis.FILE_READ:
            return not p.read_paths or any(
                _path_under(str(value), root) for root in p.read_paths
            )
        if axis is CapabilityAxis.FILE_WRITE:
            return not p.write_paths or any(
                _path_under(str(value), root) for root in p.write_paths
            )
        if axis is CapabilityAxis.NETWORK_HOST:
            return bool(p.network)
        if axis is CapabilityAxis.SUBPROCESS:
            return bool(p.allow_subprocess)
        if axis is CapabilityAxis.ENV:
            return not p.env_passthrough or value in p.env_passthrough
        # MCP / SKILL / SECRET_WRITE / PYTHON: sandbox does not constrain → ⊤.
        return True


class ProfileLayer:
    """The ALLOWLIST layer: ``AgentProfile`` agent-level allowlists. ``None`` means
    no per-agent restriction (⊤). Generalizes the existing ``allowed_mcp`` ∩
    precedent (agent-list ∩ project) to the unified model."""

    def __init__(self, profile: "AgentProfile | None") -> None:
        self._profile = profile

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        pr = self._profile
        if pr is None:
            return True
        if axis is CapabilityAxis.SKILL:
            return pr.allowed_skills is None or value in pr.allowed_skills
        if axis is CapabilityAxis.MCP:
            return pr.allowed_mcp is None or value in pr.allowed_mcp
        return True  # profile constrains only skill / mcp


def _path_under(path_str: str, root: str) -> bool:
    """True if ``path_str`` is ``root`` or a descendant (resolved). Used for the
    sandbox path caps (mirrors the recursive-scope match shape)."""
    from pathlib import Path

    try:
        p = Path(path_str).expanduser().resolve()
        r = Path(root).expanduser().resolve()
    except Exception:
        return False
    if p == r:
        return True
    try:
        p.relative_to(r)
        return True
    except ValueError:
        return False


class EffectivePermission:
    """The conjunctive-∩ resolver: a capability is permitted iff EVERY layer
    permits it. Restrict-only / grant-back forbidden is a STRUCTURAL property of
    ``all(...)`` — no layer's ``False`` can be overridden. Build per op-context
    from the live layers (Q3); cheap, no materialized sets (Q2)."""

    def __init__(self, layers: "list[LayerView]") -> None:
        self._layers = list(layers)

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        return all(layer.allows(axis, value) for layer in self._layers)

    @classmethod
    def of(
        cls,
        *,
        decl: "PermissionDecl",
        sandbox_policy: "SandboxPolicy | None" = None,
        profile: "AgentProfile | None" = None,
    ) -> "EffectivePermission":
        """Build from the four inputs (zone folded into the agent layer)."""
        return cls([
            AgentLayer(decl),
            SandboxLayer(sandbox_policy),
            ProfileLayer(profile),
        ])
