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
    from reyn.security.permissions.capability_profile import CapabilityProfile
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
    SECRET_WRITE = "secret_write"
    # PYTHON axis removed — require_python had zero production callers; the
    # preprocessor step dispatch never routed through PermissionResolver.
    ENV = "env"
    # #1199 S3.1b-2c: the per-actor tool allowlist (decl.tool) — a distinct
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
    """The GRANT layer: the agent's ``PermissionDecl`` over the default-zone
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
            # runtime host-prompt / legacy compat / per-host persistence)
            # stays in require_http_get as the non-∩ flow; this axis is just the
            # decl membership (so S3.1c can ∩ SandboxLayer.network).
            return (
                any(e.get("host") in (value, "*") for e in d.http_get)
                or self._approved(axis, value)
            )
        if axis is CapabilityAxis.MCP:
            # #1199 S3.1b: the per-skill GRANT (``decl.mcp``). #2074 S4a moved the
            # per-agent allowlist (``decl.allowed_mcp``) OUT to a ProfileLayer in
            # require_mcp (symmetric with SKILL) — so the full ∩ is now
            # ``AgentLayer(grant) ∩ ProfileLayer(allowlist) ∩ ContextualLayer``,
            # byte-identical to the prior ``grant ∩ allowlist`` (∩ associative).
            return value in d.mcp
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
            # #1199 S3.1b-2c: the per-actor tool allowlist (require_tool).
            return value in d.tool
        # ENV / SUBPROCESS / PYTHON(removed) / SKILL(removed): the decl does not constrain → ⊤.
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
        # MCP / SKILL(removed) / SECRET_WRITE / PYTHON(removed): sandbox does not constrain → ⊤.
        return True


class ProfileLayer:
    """The per-agent ALLOWLIST layer (#2074) — reads the agent's **default
    capability spec** (a :class:`CapabilityProfile`) on the MCP axis, so one
    primitive (the unified spec) feeds the binding adapter.

    The spec is ``AgentProfile.default_profile()`` where the profile is available
    (the canonical source), else built from already-extracted allowlists via
    :meth:`from_allowlists` (byte-identical — the same ``mcp_allow`` value).
    A ``None`` spec, or a ``None`` axis allow-list, is unrestricted (⊤)."""

    def __init__(self, spec: "CapabilityProfile | None") -> None:
        self._spec = spec

    @classmethod
    def from_allowlists(
        cls,
        *,
        allowed_mcp: "object | None" = None,
    ) -> "ProfileLayer":
        """Build a per-agent layer from already-extracted ``allowed_mcp`` by wrapping
        it in the canonical capability spec (#2074 S4b). ``None`` = unrestricted."""
        from reyn.security.permissions.capability_profile import CapabilityProfile

        return cls(CapabilityProfile(
            name="_per_agent_default",
            mcp_allow=tuple(allowed_mcp) if allowed_mcp is not None else None,
        ))

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        sp = self._spec
        if sp is None:
            return True
        if axis is CapabilityAxis.MCP:
            return sp.mcp_allow is None or value in sp.mcp_allow
        return True  # the per-agent spec constrains only mcp (allow-list)


@dataclass(frozen=True)
class ContextualPermission:
    """Per-session contextual narrowing (#1827) — a restrict-only ∩ term layered
    on top of the static authority (``permission.tool`` etc.). Sourced per-session
    from a delegation / topology role / ephemeral profile (later slices wire those
    sources) and carried on ``OpContext.contextual_permission``.

    Per-axis ``*_allow`` (None = unconstrained ⊤) ∩ ``¬*_deny``. The TOOL and MCP
    axes are enforced by :class:`ContextualLayer`.
    """

    tool_allow: "frozenset[str] | None" = None
    tool_deny: "frozenset[str]" = field(default_factory=frozenset)
    mcp_allow: "frozenset[str] | None" = None
    mcp_deny: "frozenset[str]" = field(default_factory=frozenset)


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
        if axis is CapabilityAxis.MCP:
            # #2074 S4a: per-context MCP narrowing (paired with the require_mcp
            # gate wiring). ⊤ when unset (mcp_allow=None + empty mcp_deny) →
            # byte-identical for any context that does not narrow MCP.
            in_allow = c.mcp_allow is None or value in c.mcp_allow
            return in_allow and value not in c.mcp_deny
        return True


def tool_contextually_denied(
    contextual: "ContextualPermission | None", effective_name: str
) -> bool:
    """The single contextual TOOL-axis gate check (#1912).

    True iff a per-session contextual narrowing is present AND denies
    ``effective_name``. **Every** tool-dispatch path calls this one function —
    chat ``RouterLoop._excluded_result``, the phase ``RouterLoop`` (same code),
    and control-IR op dispatch — so contextual enforcement is a single seam,
    bypass-impossible by construction. ``contextual is None`` → not denied (⊤),
    so an un-narrowed path is byte-identical to pre-#1827.

    Callers pass the **effective resolved name** (``invoke_action`` already
    unwrapped to ``action_name``; a control-IR op mapped to its tool-name) so the
    same name vocabulary reaches the deny-set on every path.
    """
    if contextual is None:
        return False
    return not ContextualLayer(contextual).allows(CapabilityAxis.TOOL, effective_name)


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
            # #2074 S4b: the per-agent layer reads the agent's default capability
            # spec (the unified primitive), not the AgentProfile directly.
            ProfileLayer(profile.default_profile() if profile is not None else None),
        ])
