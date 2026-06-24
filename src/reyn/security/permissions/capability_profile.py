"""Capability profile (#1827 S2a; unified spec #2074) — the named spec + resolver.

A ``capability_profile`` is a named, declarative narrowing of an agent's
capabilities, loaded from ``.reyn/capability_profiles/<name>.yaml``. It is the
**single capability-narrowing primitive** across all #1199 ∩ axes (#2074):

- **authority (enforcement)** → a :class:`ContextualPermission` carrying the
  TOOL / SKILL / MCP axes (``*_allow`` / ``*_deny``) that ride the live ∩-gate.
- **visibility (cognitive)** → an ``excluded_categories`` set derived from
  ``categories`` against the canonical catalog.

One primitive feeds TWO binding adapters (#2074): per-context (topology /
delegate / untrusted-auto, composable) and per-agent-default (AgentProfile's
allowlist baseline, #2074 S2/S4a). Both feed the UNCHANGED ``EffectivePermission``
∩ — the spec is separated from the binding.

This module is PURE — schema + loader + resolver + compose. Enforcement wiring
lives in the binding adapters: the TOOL axis rides the live gate today; SKILL /
MCP axes are carried by the resolver (#2074 S1) and consumed by the per-agent
adapter (S2) + ContextualLayer (S3). With no profile applied the session is
byte-identical to pre-#1827.

The resolver never *grants* — both products are restrict-only:
``ContextualPermission`` is an ∩ term (never-elevate is the ``all()`` in
``EffectivePermission``); ``excluded_categories`` only hides. So **visible ⊆
authorized** holds structurally.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reyn.security.permissions.effective import ContextualPermission
from reyn.tools.universal_catalog import CATEGORIES


@dataclass(frozen=True)
class CapabilityProfile:
    """A named capability narrowing (loaded from YAML).

    The single narrowing primitive across all #1199 ∩ axes (#2074):

    - ``categories`` — the catalog categories to KEEP VISIBLE (axis B). ``None``
      = no visibility narrowing (show everything). An explicit (possibly empty)
      tuple narrows the view to that set.
    - ``tool_allow`` / ``tool_deny`` — the TOOL axis (allow-list / deny-list).
    - ``skill_allow`` / ``skill_deny`` — the SKILL axis (#2074 S1). ``skill_allow``
      None = unrestricted; ``()`` = none allowed; a set = only those. This matches
      ``AgentProfile.allowed_skills`` semantics exactly (None / [] / [a,b]) so the
      per-agent binding adapter (#2074 S2/S4a) routes through it byte-identically.
    - ``mcp_allow`` / ``mcp_deny`` — the MCP axis (#2074 S1), same allow/deny shape.

    Tuples (not sets) so the dataclass stays frozen/hashable; the resolver
    converts to frozensets.
    """

    name: str
    description: str = ""
    categories: "tuple[str, ...] | None" = None
    tool_allow: "tuple[str, ...] | None" = None
    tool_deny: "tuple[str, ...]" = ()
    # #2074 S1 — the SKILL / MCP axes of the unified spec (additive).
    skill_allow: "tuple[str, ...] | None" = None
    skill_deny: "tuple[str, ...]" = ()
    mcp_allow: "tuple[str, ...] | None" = None
    mcp_deny: "tuple[str, ...]" = ()


def _as_tuple(value: "object | None") -> "tuple[str, ...] | None":
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def load_capability_profile(path: "str | Path") -> CapabilityProfile:
    """Load a ``CapabilityProfile`` from a ``.reyn/capability_profiles/<name>.yaml``.

    Unknown keys are ignored (forward-compat). ``name`` defaults to the file stem.
    A missing ``categories`` key → ``None`` (no view narrowing); a present-but-empty
    list → ``()`` (narrow the view to nothing).
    """
    import yaml

    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    return CapabilityProfile(
        name=str(data.get("name", p.stem)),
        description=str(data.get("description", "") or ""),
        categories=_as_tuple(data["categories"]) if "categories" in data else None,
        tool_allow=_as_tuple(data["tool_allow"]) if "tool_allow" in data else None,
        tool_deny=_as_tuple(data.get("tool_deny")) or (),
        # #2074 S1 — SKILL / MCP axes (additive; absent → None/() = ⊤).
        skill_allow=_as_tuple(data["skill_allow"]) if "skill_allow" in data else None,
        skill_deny=_as_tuple(data.get("skill_deny")) or (),
        mcp_allow=_as_tuple(data["mcp_allow"]) if "mcp_allow" in data else None,
        mcp_deny=_as_tuple(data.get("mcp_deny")) or (),
    )


def resolve_profile(
    profile: CapabilityProfile,
) -> "tuple[ContextualPermission, frozenset[str]]":
    """Resolve a profile into ``(ContextualPermission, excluded_categories)``.

    - enforcement: a ``ContextualPermission`` carrying the TOOL / SKILL / MCP axes
      (``*_allow`` / ``*_deny``) — the ∩ term (#2074 S1 adds SKILL / MCP).
    - view: ``excluded_categories = CATEGORIES − categories`` when ``categories``
      is set; ``∅`` (no view narrowing) when ``categories is None``.

    Unknown category names in ``categories`` are simply not in ``CATEGORIES`` and
    so do not reduce the excluded set (they are a no-op, not an error — the loader
    is forward-compat).
    """
    contextual = ContextualPermission(
        tool_allow=(
            frozenset(profile.tool_allow) if profile.tool_allow is not None else None
        ),
        tool_deny=frozenset(profile.tool_deny),
        # #2074 S1 — SKILL / MCP axes (carried; ContextualLayer enforces them in S3).
        skill_allow=(
            frozenset(profile.skill_allow) if profile.skill_allow is not None else None
        ),
        skill_deny=frozenset(profile.skill_deny),
        mcp_allow=(
            frozenset(profile.mcp_allow) if profile.mcp_allow is not None else None
        ),
        mcp_deny=frozenset(profile.mcp_deny),
    )
    if profile.categories is None:
        excluded_categories: "frozenset[str]" = frozenset()
    else:
        excluded_categories = frozenset(CATEGORIES) - frozenset(profile.categories)
    return contextual, excluded_categories


def compose_resolved(
    resolved: "list[tuple[ContextualPermission, frozenset[str]]]",
) -> "tuple[ContextualPermission, frozenset[str]]":
    """Compose N resolved profiles, most-restrictive-wins (#1827 multi-membership).

    Monotonic with the ∩ model: **union of denials, intersection of allows** —
    applied uniformly to every axis (#2074 S1: TOOL / SKILL / MCP).
    - ``*_deny`` → union (any profile's deny wins).
    - ``*_allow`` → intersection of the *set* allow-lists (``None`` = ⊤, skipped);
      a value stays allowed only if every constraining profile allows it.
    - ``excluded_categories`` → union (any profile's hide wins).

    Empty input → an inert ``(ContextualPermission(), ∅)``.
    """
    contexts = [c for (c, _excl) in resolved]

    def _compose_axis(
        allow_attr: str, deny_attr: str
    ) -> "tuple[frozenset[str] | None, frozenset[str]]":
        """Union the per-axis denies; intersect the per-axis allow-lists (None=⊤=skip)."""
        deny: "set[str]" = set()
        allow_sets: "list[frozenset[str]]" = []
        for c in contexts:
            deny |= set(getattr(c, deny_attr))
            allow = getattr(c, allow_attr)
            if allow is not None:
                allow_sets.append(allow)
        combined_allow = frozenset.intersection(*allow_sets) if allow_sets else None
        return combined_allow, frozenset(deny)

    tool_allow, tool_deny = _compose_axis("tool_allow", "tool_deny")
    skill_allow, skill_deny = _compose_axis("skill_allow", "skill_deny")
    mcp_allow, mcp_deny = _compose_axis("mcp_allow", "mcp_deny")
    excluded: "set[str]" = set()
    for _c, excl in resolved:
        excluded |= set(excl)
    return (
        ContextualPermission(
            tool_allow=tool_allow, tool_deny=tool_deny,
            skill_allow=skill_allow, skill_deny=skill_deny,
            mcp_allow=mcp_allow, mcp_deny=mcp_deny,
        ),
        frozenset(excluded),
    )


# ── #1827 S4: context-auto untrusted-source narrowing ───────────────────────
#
# Defense-in-depth with the #1862 content-fence: while untrusted external content
# is live in the agent's active context, the agent is also CAPABILITY-narrowed —
# so even a partial prompt-injection has no dangerous tools to reach. This is
# **seam-agnostic**: any untrusted-content seam stamps ``UNTRUSTED_META_KEY`` on
# its history/context entry meta (the external peer answer in S4 v1; external
# tool-results in the #1909 follow-up), and the tainted-derivation is marker-
# driven, not seam-specific.

# The marker key a seam stamps on a history-entry meta to mark untrusted content.
UNTRUSTED_META_KEY: "str" = "external_source"

# The well-known auto-applied profile name. An operator
# ``.reyn/capability_profiles/_untrusted.yaml`` overrides the built-in secure
# default — an override is a *deliberate loosening*, never a tightening of the
# floor below what the operator opts into.
UNTRUSTED_PROFILE_NAME: "str" = "_untrusted"

# The built-in secure default: deny the side-effecting / persistence /
# re-delegation / execution / install surfaces so untrusted content can be read
# and reasoned about but cannot drive irreversible actions. Both the qualified
# catalog names and their unwrapped aliases are denied (the live gate matches the
# effective resolved name, which differs by scheme / invoke_action unwrap).
#
# Grouped by CLASS (#2081 S3): the runtime FLOOR is the flat union below; the
# delegation-unsafe AUDIT (DELEGATION_AUDIT_CLASSES) derives its FLOORED classes +
# severities from these same groups — so the floor and the audit cannot drift apart.
#
# #2111 (CRITICAL fix): each class is defined by its QUALIFIED (universal-catalog)
# names; the bare/unwrapped aliases are DERIVED from the invoke_action unwrap
# source-of-truth (``unwrapped_tool_name``) — so EVERY invocable form (bare AND
# qualified) is denied on EVERY scheme path, COMPLETE-BY-CONSTRUCTION. (The prior
# manual lists missed the bare memory-write + mcp-install aliases — a delegate / an
# untrusted-content turn could call bare ``remember_shared`` and persist to shared
# memory. Deriving kills that gap-class at the root: a new floored tool can't miss
# its alias.) Shared by BOTH floors (builtin_untrusted_profile + builtin_delegate_
# profile), so this closes the gap on both surfaces.
_FLOORED_QUALIFIED: "dict[str, frozenset[str]]" = {
    # memory writes / deletes — no persistence from untrusted content
    "memory-write": frozenset({
        "memory_operation__remember_shared",
        "memory_operation__remember_agent",
        "memory_operation__forget",
    }),
    # re-delegation — no spawning peers from untrusted content
    "re-delegation": frozenset({"multi_agent__delegate"}),
    # code execution
    "exec": frozenset({"exec__sandboxed_exec"}),
    # MCP install — no installing servers from untrusted content
    "mcp-install": frozenset({
        "mcp__install_registry", "mcp__install_package", "mcp__install_local",
    }),
    # session/agent spawn — no spawning sub-sessions from untrusted content / an
    # unbound delegate (#2103 S1bc: unbounded sub-session spawn is a DoS vector; the
    # ⊆-parent model blocks escalation, but spawning itself is restrict-floored like
    # re-delegation). ``session_spawn`` is a router-only tool with NO invoke_action
    # route today, so it is BARE-ONLY (no qualified→bare unwrap alias); a future
    # qualified route would be floored by the same _with_unwrapped_aliases derivation.
    "spawn": frozenset({"session_spawn"}),
}


def _with_unwrapped_aliases(qualified: "frozenset[str]") -> "frozenset[str]":
    """Each qualified name PLUS its bare unwrapped alias (from the invoke_action
    source-of-truth) — every invocable form, complete-by-construction (#2111)."""
    from reyn.tools.universal_dispatch import unwrapped_tool_name
    forms = set(qualified)
    for q in qualified:
        bare = unwrapped_tool_name(q)
        if bare is not None:
            forms.add(bare)
    return frozenset(forms)


_FLOORED_DENY_CLASSES: "dict[str, frozenset[str]]" = {
    cls: _with_unwrapped_aliases(q) for cls, q in _FLOORED_QUALIFIED.items()
}
_BUILTIN_UNTRUSTED_DENY: "frozenset[str]" = frozenset().union(*_FLOORED_DENY_CLASSES.values())


def builtin_untrusted_profile() -> CapabilityProfile:
    """The built-in secure default auto-applied while untrusted content is live."""
    return CapabilityProfile(
        name=UNTRUSTED_PROFILE_NAME,
        description="auto-applied while untrusted external content is in context (#1827 S4)",
        tool_deny=tuple(sorted(_BUILTIN_UNTRUSTED_DENY)),
    )


def load_untrusted_profile(project_root: "str | Path") -> CapabilityProfile:
    """The minimal profile auto-applied while untrusted external content is live.

    An operator ``.reyn/capability_profiles/_untrusted.yaml`` overrides the
    built-in secure default (a deliberate loosening). A malformed override falls
    back to the built-in (surfaced on stderr) — a typo must not silently drop the
    untrusted floor.
    """
    path = Path(project_root) / ".reyn" / "capability_profiles" / f"{UNTRUSTED_PROFILE_NAME}.yaml"
    if path.is_file():
        try:
            return load_capability_profile(path)
        except Exception as e:  # noqa: BLE001 — fall back to the secure default
            import sys
            print(
                f"warning: malformed {path.name}: {e} — using the built-in "
                "untrusted default",
                file=sys.stderr,
            )
    return builtin_untrusted_profile()


# ── #2081: the restrictive floor for an unbound delegate ────────────────────
#
# delegation.capability_default=deny narrows an UNBOUND delegate (one spawned by
# another agent's delegation, recursively) with this profile, unless a topology
# capability_profile binding re-grants it (the binding REPLACES the default, since
# composition is most-restrictive-wins). The NAME is decoupled from ``_untrusted``
# (delegate-spawn vs untrusted-content are distinct contexts) but the default
# taxonomy is the SAME single-sourced ``_BUILTIN_UNTRUSTED_DENY`` set — so operators
# can tune delegate-deny independently via ``.reyn/capability_profiles/_delegate.yaml``.

# The well-known auto-applied delegate-floor profile name.
DELEGATE_PROFILE_NAME: "str" = "_delegate"


def builtin_delegate_profile() -> CapabilityProfile:
    """The built-in restrictive floor auto-applied to an unbound delegate under
    ``delegation.capability_default=deny`` (#2081). Reuses the single-sourced
    ``_BUILTIN_UNTRUSTED_DENY`` taxonomy (re-delegation / side-effect-exec /
    memory-write / MCP-install)."""
    return CapabilityProfile(
        name=DELEGATE_PROFILE_NAME,
        description="auto-applied to an unbound delegate under delegation.capability_default=deny (#2081)",
        tool_deny=tuple(sorted(_BUILTIN_UNTRUSTED_DENY)),
    )


def load_delegate_profile(project_root: "str | Path") -> CapabilityProfile:
    """The restrictive profile auto-applied to an unbound delegate (#2081).

    An operator ``.reyn/capability_profiles/_delegate.yaml`` overrides the built-in
    secure default (a deliberate loosening). A malformed override falls back to the
    built-in (surfaced on stderr) — a typo must not silently drop the delegate floor.
    """
    path = Path(project_root) / ".reyn" / "capability_profiles" / f"{DELEGATE_PROFILE_NAME}.yaml"
    if path.is_file():
        try:
            return load_capability_profile(path)
        except Exception as e:  # noqa: BLE001 — fall back to the secure default
            import sys
            print(
                f"warning: malformed {path.name}: {e} — using the built-in "
                "delegate default",
                file=sys.stderr,
            )
    return builtin_delegate_profile()


# ── #2081 S3: the delegation-unsafe AUDIT taxonomy ──────────────────────────
#
# ``reyn audit`` (gateway:delegation-unsafe) flags, per dangerous CLASS, a
# delegate-REACHABLE bound capability_profile — or the ``_delegate.yaml`` override —
# that PERMITS the class (a re-grant that widens an unbound delegate's floor; the floor
# is REPLACED by a binding, so even a floored class can be re-granted).
#
# The FLOORED classes (re-delegation / exec / mcp-install / memory-write) are
# single-sourced from ``_FLOORED_DENY_CLASSES`` + the severity map below — so the audit
# and the runtime floor cannot drift. ``destructive-fs`` is an explicit, documented
# AUDIT-ONLY class (the intentional audit ⊋ floor delta): ``delete_file`` is a
# delegate-reachable concern, but it is FILE_WRITE-permission-bounded so it is not on
# the runtime floor — the audit surfaces it as a re-grant judgment regardless.
_FLOORED_AUDIT_SEVERITY: "dict[str, str]" = {
    "re-delegation": "HIGH",
    "exec": "HIGH",
    "mcp-install": "HIGH",
    "memory-write": "MED",
    "spawn": "HIGH",  # #2103: unbounded sub-session spawn (DoS) — peer of re-delegation
}
DELEGATION_AUDIT_CLASSES: "dict[str, tuple[str, frozenset[str]]]" = {
    cls: (_FLOORED_AUDIT_SEVERITY[cls], tools)
    for cls, tools in _FLOORED_DENY_CLASSES.items()
}
DELEGATION_AUDIT_CLASSES["destructive-fs"] = ("MED", frozenset({"delete_file", "file__delete"}))


def profile_permits(profile: CapabilityProfile, tool: str) -> bool:
    """Whether ``profile`` would PERMIT ``tool`` on the TOOL axis — the allow-list is
    satisfied (None = unconstrained, else membership) AND it is not denied. The
    delegation-unsafe audit's re-grant check (#2081 S3)."""
    in_allow = profile.tool_allow is None or tool in profile.tool_allow
    return in_allow and tool not in profile.tool_deny


def metas_have_untrusted(metas: "object") -> bool:
    """Seam-agnostic taint check: True iff any entry meta carries the untrusted
    marker. Derived from the **active** context (the caller passes the live,
    un-compacted entries), which gives the until-compaction scope for free —
    a compacted-out untrusted entry is simply not present."""
    try:
        return any(
            isinstance(m, dict) and m.get(UNTRUSTED_META_KEY) for m in metas  # type: ignore[union-attr]
        )
    except TypeError:
        return False
