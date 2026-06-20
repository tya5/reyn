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

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from reyn.security.permissions.permissions import (
    _in_default_read_zone,
    _in_default_write_zone,
)

if TYPE_CHECKING:
    from reyn.runtime.profile import AgentProfile
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.security.sandbox.policy import SandboxPolicy


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
    # #1199 S3.1b-2c: the per-skill tool allowlist (decl.tool) — a distinct
    # capability axis (gated by require_tool) not in the original 9; added here
    # for the require_tool cutover.
    TOOL = "tool"


class LayerView(Protocol):
    """One permission layer's projection. ``allows(axis, value)`` answers, for a
    concrete request value (a path / host / name / (module, function) / env var),
    whether THIS layer permits it. A layer that does not constrain ``axis``
    returns ``True`` (⊤) so it never narrows the conjunction."""

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:  # pragma: no cover
        ...


class AgentLayer:
    """The GRANT layer: the skill's ``PermissionDecl`` over the default-zone
    baseline, faithful to the ``require_*`` gate logic (reuses the same helpers).

    Runtime approvals are folded IN here (#1199 S3.1b ② — NOT a top-level
    ``approved OR effective`` disjunct, which would let an approval re-grant what
    a downstream Sandbox/Profile layer denies = a grant-back hole in the full ∩):

    - ``approval_check(axis, value) -> bool``: the startup/config approvals
      (``_is_config_approved`` / ``_is_path_approved_for``). Folded into the agent
      allow-set, so ``effective = AgentLayer(…, approvals) ∩ Sandbox ∩ Profile``
      lets the conjunction restrict approvals too (grant-back forbidden preserved).

    #1199 S3.1c-1: the FILE axes are **decl-less** — a file path is permitted iff
    it is in the default zone OR explicitly approved. The skill's declared file
    paths are NOT auto-granted (the prior non-interactive ``decl_covers`` disjunct
    + the ``include_decl`` flag are gone). This resolves the S3.1b-2 transitional
    divergence: ``require_file_*`` (op-runtime) and ``is_read/write_allowed``
    (Workspace) now make the SAME decision. A non-interactive declared-but-
    unapproved path therefore denies (the operator pre-approves via reyn.yaml or
    runs interactively). Non-file axes still consult the decl below.
    """

    def __init__(
        self,
        decl: "PermissionDecl",
        *,
        approval_check: "Any" = None,
        file_zone_root: "Any" = None,
    ) -> None:
        self._decl = decl
        self._approval_check = approval_check
        # #1316/#1414: the root the default file zones are anchored to. None →
        # Path.cwd() inside the zone fns (historical). This is the FILE-ZONE
        # root only — under a container backend (#1414) it is the in-container
        # repo root (workspace_base_dir), which may DIVERGE from the host-side
        # approvals base (the resolver passes ``_file_zone_root``, defaulting to
        # the host ``_project_root`` so host/interactive stays byte-identical).
        self._file_zone_root = file_zone_root

    def _approved(self, axis: CapabilityAxis, value: Any) -> bool:
        return bool(self._approval_check and self._approval_check(axis, value))

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        d = self._decl
        if axis is CapabilityAxis.FILE_READ:
            # #1199 S3.1c-1: decl-less — zone OR approved (no decl auto-grant).
            return (
                _in_default_read_zone(str(value), self._file_zone_root)
                or self._approved(axis, value)
            )
        if axis is CapabilityAxis.FILE_WRITE:
            # #1199 S3.1c-1: decl-less — zone OR approved (no decl auto-grant).
            return (
                _in_default_write_zone(str(value), self._file_zone_root)
                or self._approved(axis, value)
            )
        if axis is CapabilityAxis.NETWORK_HOST:
            # #1199 S3.1b-2c-2: faithful to require_http_get's membership decision —
            # a specific declared host OR the "*" wildcard (host set unknown at
            # write-time). The intricate resolution flow (config-deny tiers /
            # startup_guard host-prompt / legacy compat / per-host persistence)
            # stays in require_http_get as the non-∩ flow; this axis is just the
            # decl membership (so S3.1c can ∩ SandboxLayer.network).
            return (
                any(e.get("host") in (value, "*") for e in d.http_get)
                or self._approved(axis, value)
            )
        if axis is CapabilityAxis.MCP:
            # #1199 S3.1b: faithful to require_mcp (permissions.py:1248+1253) —
            # the per-skill grant (``decl.mcp``) AND the per-skill allowlist
            # (``decl.allowed_mcp``: None = no restriction). The per-AGENT
            # allowlist (AgentProfile.allowed_mcp) is the separate ProfileLayer.
            in_grant = value in d.mcp
            in_allowlist = d.allowed_mcp is None or value in d.allowed_mcp
            return in_grant and in_allowlist
        if axis is CapabilityAxis.SECRET_WRITE:
            # #1199 S3.1b-2c: faithful to require_secret_write — a specific key OR
            # the "*" wildcard (runtime-determined keys, gated by the per-value
            # op-execution prompt). _approved kept for symmetry (no current
            # secret approval source, but harmless).
            return (
                value in d.secret_write
                or "*" in d.secret_write
                or self._approved(axis, value)
            )
        if axis is CapabilityAxis.TOOL:
            # #1199 S3.1b-2c: the per-skill tool allowlist (require_tool).
            return value in d.tool
        if axis is CapabilityAxis.PYTHON:
            # value = (module, function)
            return any(
                (p.module, p.function) == tuple(value) for p in d.python
            )
        # SKILL / ENV / SUBPROCESS: the decl does not constrain → ⊤.
        # (#1352-L3: the shell-permission SUBPROCESS gate was retired with the
        # shell op; subprocess is now bounded by SandboxLayer.allow_subprocess
        # at the sandboxed_exec seam, not the AgentLayer.)
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


@dataclass(frozen=True)
class ContextualPermission:
    """Per-session contextual narrowing (#1827) — a restrict-only ∩ term layered
    on top of the static authority (``permission.tool`` etc.). Sourced per-session
    from a delegation / topology role / ephemeral profile (later slices wire those
    sources) and carried on ``OpContext.contextual_permission``.

    S1 covers the TOOL axis: ``tool_allow`` (None = unconstrained) ∩ ``¬tool_deny``.
    Further axes are added as later slices wire them — until then this term is ⊤
    on those axes (never narrows).
    """

    tool_allow: "frozenset[str] | None" = None
    tool_deny: "frozenset[str]" = field(default_factory=frozenset)


class ContextualLayer:
    """The CONTEXTUAL ∩ layer (#1827): per-session narrowing from a delegation /
    topology / ephemeral context.

    never-elevate is **structural**, not a runtime check: a ``ContextualLayer`` is
    just one more conjunct in :meth:`EffectivePermission.allows` (``all(...)``), so
    it can only contribute ``False`` (narrow) and **no other layer's ``True`` can
    re-grant what it denies, nor can it re-grant a lower layer's ``False``**. A
    ``None`` context is ⊤ (the layer is inert → byte-identical to the pre-#1827
    stack)."""

    def __init__(self, contextual: "ContextualPermission | None") -> None:
        self._ctx = contextual

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        c = self._ctx
        if c is None:
            return True
        if axis is CapabilityAxis.TOOL:
            in_allow = c.tool_allow is None or value in c.tool_allow
            not_denied = value not in c.tool_deny
            return in_allow and not_denied
        # S1: only TOOL is constrained; other axes are ⊤ until later slices wire
        # them (so the ∩ never narrows an axis the context does not yet cover).
        return True


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
        approval_check: "Any" = None,
        file_zone_root: "Any" = None,
    ) -> "EffectivePermission":
        """Build from the inputs (zone + approvals folded into the agent
        layer; ② grant-back-safe). Build per op-context (Q3).

        #1316/#1414: ``file_zone_root`` anchors the default file zones (None →
        cwd). Distinct from the host approvals base under a container backend."""
        return cls([
            AgentLayer(decl, approval_check=approval_check, file_zone_root=file_zone_root),
            SandboxLayer(sandbox_policy),
            ProfileLayer(profile),
        ])
