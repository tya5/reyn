"""Effective-permission model — #1199 S3.1 conjunctive-∩ invariant (S3.1a).

The OS-level permission rule (#1115 / #1199 S3.1): a capability is permitted iff
EVERY permission layer permits it — **effective = ⋂ layers, restrict-only,
grant-back forbidden**. No layer's deny can be re-granted by another layer; ∩ can
only narrow.

Four inputs, three ⋂ layers:
- **agent** (`PermissionDecl` + the configured file scope): the GRANT layer. Its
  allow-set is the configured file scope (#3458 — `permissions.file.read` /
  `file.write`, whose schema default is the symbolic zone) ∪ the actor's explicit
  declarations. The scope is folded in here as the baseline — NOT a separate ∩
  restrictor (a separate restrictor would cancel the decl grants that
  intentionally extend beyond it).
- **sandbox** (`SandboxPolicy`): runtime caps (paths / network / subprocess / env).
- **profile** (`AgentProfile`): agent-level allowlists (agents / mcp).

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

from reyn.security.permissions.file_scope import FileScopes, resolve_file_scopes

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
    # #3198: skill-load ``${env:VAR}`` expansion allowlist — DISTINCT from
    # ``ENV`` above (that axis gates which env-var NAMES pass THROUGH to a
    # sandboxed subprocess — ``SandboxPolicy.env_deny_names``, #3901 PR-B ④
    # renamed the sandbox side of this to a deny-list; this axis
    # gates whether a SKILL.md body may read a name FROM os.environ into
    # the LLM's context at all). Same vocabulary, deliberately different
    # capability — conflating them would let a subprocess env-passthrough
    # declaration silently double as a credential-exposure grant.
    ENV_EXPAND = "env_expand"
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
    """The GRANT layer: the agent's ``PermissionDecl`` over the configured
    file-scope baseline, faithful to the ``require_*`` gate logic.

    Runtime approvals are folded IN here (#1199 S3.1b ② — NOT a top-level
    ``approved OR effective`` disjunct, which would let an approval re-grant what
    a downstream Sandbox/Profile layer denies = a grant-back hole in the full ∩):

    - ``approval_check(axis, value) -> bool``: the startup/config approvals
      (``_is_config_approved`` / ``_is_path_approved_for``). Folded into the agent
      allow-set, so ``effective = AgentLayer(…, approvals) ∩ Sandbox ∩ Profile``
      lets the conjunction restrict approvals too (grant-back forbidden preserved).

    #1199 S3.1c-1: the FILE axes are **decl-less** — a file path is permitted iff
    it is in the configured scope (#3458: ``permissions.file.read`` /
    ``file.write``, defaulting via the schema to the symbolic zone; the layer
    itself holds no default) OR explicitly approved. The actor's declared file
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
        file_scopes: "FileScopes | None" = None,
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
        # #3458: the resolved path sets for the FILE axes. This layer holds NO
        # default of its own — an omitted ``file_scopes`` is resolved from an
        # empty config by the SAME function the gate and the advertisement call,
        # so the schema default (``file_scope.FILE_SCOPE_SCHEMA``) is the only
        # place a default zone is written down.
        self._file_scopes = file_scopes or resolve_file_scopes(
            {}, zone_root=file_zone_root,
        )

    def _approved(self, axis: CapabilityAxis, value: Any) -> bool:
        return bool(self._approval_check and self._approval_check(axis, value))

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        d = self._decl
        if axis is CapabilityAxis.FILE_READ:
            # #1199 S3.1c-1: decl-less. #3458: the permitted set comes from the
            # configured scope (schema default when unset) — no zone hardcoded here.
            return (
                self._file_scopes.read.contains(str(value))
                or self._approved(axis, value)
            )
        if axis is CapabilityAxis.FILE_WRITE:
            # #1199 S3.1c-1: decl-less. #3458: see FILE_READ above.
            return (
                self._file_scopes.write.contains(str(value))
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
            # #1199 S3.1b: the per-actor GRANT (``decl.mcp``). #2074 S4a moved the
            # per-agent allowlist (``decl.allowed_mcp``) OUT to a ProfileLayer in
            # require_mcp (symmetric with AGENT) — so the full ∩ is now
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
        if axis is CapabilityAxis.ENV_EXPAND:
            # #3198: faithful to secret_write's shape — a specific declared
            # name OR the "*" wildcard. Deny-by-default: an empty/unset
            # decl.env_expand denies every name (⊥ for this axis, NOT ⊤ —
            # this is the one axis where an undeclared decl must NOT fall
            # through to "unconstrained", since that would restore the
            # pre-#3198 unconditional os.environ read).
            return value in d.env_expand or "*" in d.env_expand or self._approved(axis, value)
        if axis is CapabilityAxis.SUBPROCESS:
            # #3901 PR-B ①: the actor's OWN declared intent — "may this
            # agent launch a subprocess at all". Compat default (True):
            # d.subprocess defaults to True, so an actor with no opinion
            # is unconstrained here (⊤) and the decision falls to
            # SandboxLayer's own (also compat-default) deny_subprocess.
            return bool(d.subprocess)
        if axis is CapabilityAxis.ENV:
            # #3901 PR-B ①: the actor's declared env-var-name allowlist for
            # subprocess passthrough. Compat: an empty/unset decl.env is ⊤
            # (unconstrained) here, NOT deny — this axis's restriction comes
            # from SandboxLayer.env_deny_names (a BLOCKLIST), not from this
            # allowlist being populated. Declaring specific names here
            # narrows what THIS actor may pass through even before sandbox's
            # deny-list is consulted (the ∩ still applies both ways).
            return not d.env or value in d.env
        # PYTHON(removed) / SKILL(removed): the decl does not constrain → ⊤.
        return True


class SandboxLayer:
    """The RESTRICT layer for the axes ``SandboxPolicy`` actually gates in
    the permission ∩ (#3901 PR-B ③): SUBPROCESS, ENV, and NETWORK_HOST — values
    an operator declares AS permission (#3901 PR-B ①, ``PermissionDecl.subprocess``
    / ``.env``; ``network`` via ``reyn.yaml sandbox.policy``) but that ALSO
    need a runtime floor an operator's decl cannot override downward (mirrors
    AgentLayer ∩ SandboxLayer's existing shape for every other axis).

    FILE_READ / FILE_WRITE no longer participate here (#3901 PR-B ③, owner's
    split: sandbox's job is bounding what happens BEHIND a permitted action,
    using values — like the workspace floor ``resolve_sandbox_policy``
    builds — the OPERATOR CANNOT KNOW and therefore cannot express as
    permission; #3901 §1). ``SandboxPolicy`` STILL enforces these two at the
    kernel-backend level (Seatbelt/Landlock read ``policy.write_paths``
    directly to build the actual SBPL/LSM rules) — what changed is that a
    kernel-enforced restriction on these two axes no longer ALSO narrows the
    permission ⋂ an operator's own decl computes.

    NETWORK_HOST is explicitly NOT in that retirement (lead-coder ruling,
    #3901 thread, superseding an earlier draft that grouped it with the two
    above): ``network`` is a value an operator writes directly into
    ``reyn.yaml sandbox.policy``, not a workspace floor they cannot know — the
    same shape as SUBPROCESS/ENV, not the same shape as the path caps.
    ``docs/concepts/architecture/sandbox-vs-permission.md`` and
    ``docs/concepts/runtime/sandbox.md`` both document network as the
    permission-∩-participating exfiltration gate; retiring it would falsify
    both docs in the same PR that claims to keep them in sync.

    An empty deny-list means the policy declares no restriction on that axis
    (⊤) — restrict-only, matching the rest of this model: a policy narrows
    only by listing what it denies."""

    def __init__(self, policy: "SandboxPolicy | None") -> None:
        self._policy = policy

    def allows(self, axis: CapabilityAxis, value: Any) -> bool:
        p = self._policy
        if p is None:
            return True  # no sandbox layer → unrestricted
        if axis is CapabilityAxis.SUBPROCESS:
            # #3901 PR-B ④: deny_subprocess (renamed from allow_subprocess,
            # inverted sense) — False (compat default) means unconstrained.
            return not p.deny_subprocess
        if axis is CapabilityAxis.ENV:
            # #3901 PR-B ④: env_deny_names (renamed from env_passthrough,
            # inverted from an allow-list to a deny-list) — an empty/unset
            # deny-list is ⊤ (compat: nothing extra denied), narrowed only
            # by what is explicitly listed.
            return value not in p.env_deny_names
        if axis is CapabilityAxis.NETWORK_HOST:
            # Unchanged from pre-#3901: network is an operator-declared value
            # (not a workspace floor), so it stays in the permission ∩.
            return bool(p.network)
        # FILE_READ / FILE_WRITE (#3901 PR-B ③) / MCP / SKILL(removed) /
        # SECRET_WRITE / PYTHON(removed): sandbox no longer constrains the
        # permission ∩ on these axes → ⊤.
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
class NarrowingOrigin:
    """Why one contextual narrowing term exists, in the three parts a denied
    caller needs (#3501).

    A deny that only says *that* it was denied is not actionable. Three parts
    make it actionable, and all three are required:

    - ``label`` — WHICH narrowing this is, named so it can be looked up.
    - ``cause`` — WHY it is currently active.
    - ``lifts_when`` — WHAT would remove it (a condition, a config key, or both).

    Naming several *candidate* narrowings instead of the one that fired is the
    #3501 defect: the deny listed ``delegation / topology / ephemeral`` and left
    the reader to guess, so the caller — an LLM, mid-turn — could not explain why
    a capability it had been using vanished, and could not act to get it back.
    """

    label: str
    cause: str
    lifts_when: str

    def explain(self) -> str:
        """The three parts as one sentence-run, for embedding in a deny message."""
        return f"{self.label}. Cause: {self.cause}. Lifts when: {self.lifts_when}"


@dataclass(frozen=True)
class ContextualPermission:
    """Per-session contextual narrowing (#1827) — a restrict-only ∩ term layered
    on top of the static authority (``permission.tool`` etc.). Sourced per-session
    from a delegation / topology role / ephemeral profile (later slices wire those
    sources) and carried on ``OpContext.contextual_permission``.

    Per-axis ``*_allow`` (None = unconstrained ⊤) ∩ ``¬*_deny``. The TOOL and MCP
    axes are enforced by :class:`ContextualLayer`.

    ``origin`` / ``composed_from`` carry the PROVENANCE the deny message needs
    (#3501). ∩-composition flattens N terms into one value, which erases which
    term contributed a given deny — so the composed value keeps its terms, each
    with its own :class:`NarrowingOrigin`, and :func:`attribute_deny` walks them
    to answer "which narrowing rejected this name". A leaf term has
    ``composed_from=()`` and IS its own single term; ``origin=None`` means a term
    was built without provenance (nothing in ``src/`` does — see
    ``tests/test_3501_untrusted_narrowing_opt_in.py``'s coverage arm — but a
    hand-built term stays legal and degrades to the generic message).
    """

    tool_allow: "frozenset[str] | None" = None
    tool_deny: "frozenset[str]" = field(default_factory=frozenset)
    mcp_allow: "frozenset[str] | None" = None
    mcp_deny: "frozenset[str]" = field(default_factory=frozenset)
    origin: "NarrowingOrigin | None" = None
    composed_from: "tuple[ContextualPermission, ...]" = ()


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
    """The contextual TOOL-axis gate check (#1912).

    True iff a per-session contextual narrowing is present AND denies
    ``effective_name``. ``contextual is None`` → not denied (⊤), so an
    un-narrowed path is byte-identical to pre-#1827.

    **Measured callers** (#3513, ``src/`` enumeration; #3546 adds the last one):
    the RouterLoop enforcement gate ``_excluded_result`` (chat and phase are the
    same code) and its advertisement filter, the three exposure/fence schemes
    (``_category_exposure``, ``_enumerate_exposure``,
    ``retrieval_content_fence``), and the pipeline tool-step dispatch
    (``tools/pipeline_verbs._make_tool_dispatch``). Those paths share this one
    function, so they cannot disagree about what a narrowing means. This is an
    enumeration of who calls it — NOT a claim that every path which executes a
    capability calls it.

    ⚠️ This docstring used to claim that **every** tool-dispatch path calls this
    function — naming "control-IR op dispatch" as one of them — and concluded
    contextual enforcement was "bypass-impossible by construction". That leg was
    ``core/op_runtime/contextual_gate``, whose own two consumers
    (``control_ir_executor`` / ``preprocessor_executor``) were deleted as whole
    files in #2434; the orphaned wrapper had no ``src/`` caller and was deleted
    in #3513. #3546 measured ONE of the paths that claim left open — the pipeline
    tool-step dispatch, which executed a narrowing-denied tool's real side effect
    and now calls this function. **The op-dispatch axis itself (control-IR ops,
    whose own contextual gating rides ``OpContext.contextual_permission`` rather
    than this predicate) is still unmeasured.** Do not restore an exhaustiveness
    claim here without an enumeration that supports it.

    Callers pass the **effective resolved name** (``invoke_action`` already
    unwrapped to ``action_name``) so the same name vocabulary reaches the
    deny-set on every path that calls this.
    """
    if contextual is None:
        return False
    return not ContextualLayer(contextual).allows(CapabilityAxis.TOOL, effective_name)


def narrowing_terms(
    contextual: "ContextualPermission",
) -> "tuple[ContextualPermission, ...]":
    """The individual ∩ terms behind ``contextual`` (#3501).

    A composed value reports the terms it was composed from; a leaf term reports
    itself. ``compose_resolved`` flattens on the way in, so this is one level deep
    by construction and needs no recursion."""
    return contextual.composed_from or (contextual,)


def attribute_deny(
    contextual: "ContextualPermission | None",
    axis: CapabilityAxis,
    value: str,
) -> "NarrowingOrigin | None":
    """The origin of the FIRST term that rejects ``value`` on ``axis`` (#3501).

    ``None`` when nothing rejects it, or when the rejecting term carries no
    origin. Order is composition order, which is the order the narrowings were
    layered — so the outermost/most-durable narrowing is reported first when
    several deny the same name, matching the #3380 rule that the un-liftable
    reason is the actionable one."""
    if contextual is None:
        return None
    for term in narrowing_terms(contextual):
        if not ContextualLayer(term).allows(axis, value):
            return term.origin
    return None


# Fallback when a rejecting term carries no origin: name the surfaces a narrowing
# can come from rather than asserting one of them fired. This is deliberately the
# WEAK message — every production term attaches an origin, so reaching this text
# means a term was constructed outside those paths.
_UNATTRIBUTED_NARROWING = (
    "the active capability narrowing, whose source is not recorded on the term "
    "that rejected it. A narrowing can come from a topology capability_profile "
    "binding, the `_delegate` floor, a per-session capability config, the "
    "`/visibility` override, or the `_untrusted` context narrowing"
)


def contextual_deny_message(
    subject: str,
    name: str,
    contextual: "ContextualPermission | None",
    axis: CapabilityAxis = CapabilityAxis.TOOL,
) -> str:
    """The one legible contextual-deny explanation, shared by every deny site (#3501).

    ``subject`` is the noun the caller uses (``"tool"`` / ``"MCP server"`` are the
    two live ones); ``name`` is the resolved name that was rejected. The text names
    the narrowing that fired, why it is active, and what lifts it — the three
    parts of :class:`NarrowingOrigin`.

    One function so the router-loop gate, the ``require_tool`` gate and the
    ``require_mcp`` gate cannot drift into three differently-informative denies
    for the same decision. Each of the three used to build its own string, and
    all three named the same three candidate narrowings without naming which one
    fired."""
    origin = attribute_deny(contextual, axis, name)
    reason = origin.explain() if origin is not None else _UNATTRIBUTED_NARROWING
    return f"{subject} {name!r} is not available here — blocked by {reason}."


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
        file_scopes: "FileScopes | None" = None,
    ) -> "EffectivePermission":
        """Build from the inputs (file scope + approvals folded into the agent
        layer; ② grant-back-safe). Build per op-context (Q3).

        #1316/#1414: ``file_zone_root`` anchors the file zone symbols (None →
        cwd). Distinct from the host approvals base under a container backend.
        #3458: ``file_scopes`` is the resolved ``permissions.file.*`` set; None →
        resolved from an empty config (= the schema default) at that anchor."""
        return cls([
            AgentLayer(decl, approval_check=approval_check,
                       file_zone_root=file_zone_root, file_scopes=file_scopes),
            SandboxLayer(sandbox_policy),
            # #2074 S4b: the per-agent layer reads the agent's default capability
            # spec (the unified primitive), not the AgentProfile directly.
            ProfileLayer(profile.default_profile() if profile is not None else None),
        ])
