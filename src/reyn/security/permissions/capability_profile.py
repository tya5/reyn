"""Capability profile (#1827 S2a; unified spec #2074) — the named spec + resolver.

A ``capability_profile`` is a named, declarative narrowing of an agent's
capabilities, loaded from ``.reyn/capability_profiles/<name>.yaml``. It is the
**single capability-narrowing primitive** across all #1199 ∩ axes (#2074):

- **authority (enforcement)** → a :class:`ContextualPermission` carrying the
  TOOL / MCP axes (``*_allow`` / ``*_deny``) that ride the live ∩-gate.
- **visibility (cognitive)** → an ``excluded_categories`` set derived from
  ``categories`` against the canonical catalog.

One primitive feeds TWO binding adapters (#2074): per-context (topology /
delegate / untrusted-auto, composable) and per-agent-default (AgentProfile's
allowlist baseline, #2074 S2/S4a). Both feed the UNCHANGED ``EffectivePermission``
∩ — the spec is separated from the binding.

This module is PURE — schema + loader + resolver + compose. Enforcement wiring
lives in the binding adapters: the TOOL axis rides the live gate today; the MCP
axis is carried by the resolver and consumed by the per-agent adapter (S2) +
ContextualLayer (S4a). With no profile applied the session is byte-identical to
pre-#1827.

The resolver never *grants* — both products are restrict-only:
``ContextualPermission`` is an ∩ term (never-elevate is the ``all()`` in
``EffectivePermission``); ``excluded_categories`` only hides. So **visible ⊆
authorized** holds structurally.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reyn.security.permissions.effective import ContextualPermission, NarrowingOrigin
from reyn.tools.universal_catalog import CATEGORIES


@dataclass(frozen=True)
class CapabilityProfile:
    """A named capability narrowing (loaded from YAML).

    The single narrowing primitive across all #1199 ∩ axes (#2074):

    - ``categories`` — the catalog categories to KEEP VISIBLE (axis B). ``None``
      = no visibility narrowing (show everything). An explicit (possibly empty)
      tuple narrows the view to that set.
    - ``tool_allow`` / ``tool_deny`` — the TOOL axis (allow-list / deny-list).
    - ``mcp_allow`` / ``mcp_deny`` — the MCP axis, same allow/deny shape.

    Tuples (not sets) so the dataclass stays frozen/hashable; the resolver
    converts to frozensets.
    """

    name: str
    description: str = ""
    categories: "tuple[str, ...] | None" = None
    tool_allow: "tuple[str, ...] | None" = None
    tool_deny: "tuple[str, ...]" = ()
    # #2074 S1 — the MCP axis of the unified spec.
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
        mcp_allow=_as_tuple(data["mcp_allow"]) if "mcp_allow" in data else None,
        mcp_deny=_as_tuple(data.get("mcp_deny")) or (),
    )


def resolve_profile(
    profile: CapabilityProfile,
    *,
    origin: "NarrowingOrigin | None" = None,
) -> "tuple[ContextualPermission, frozenset[str]]":
    """Resolve a profile into ``(ContextualPermission, excluded_categories)``.

    - enforcement: a ``ContextualPermission`` carrying the TOOL / MCP axes
      (``*_allow`` / ``*_deny``) — the ∩ term.
    - view: ``excluded_categories = CATEGORIES − categories`` when ``categories``
      is set; ``∅`` (no view narrowing) when ``categories is None``.

    Unknown category names in ``categories`` are simply not in ``CATEGORIES`` and
    so do not reduce the excluded set (they are a no-op, not an error — the loader
    is forward-compat).

    ``origin`` (#3501) is the BINDING's provenance, supplied by the adapter that
    decided to apply this profile — not read off the profile, because the same
    profile file applied for two different reasons lifts under two different
    conditions. It rides the resolved term so a deny site can name it.
    """
    # #3429: the TOOL axis is used verbatim. #2132 used to expand every name to
    # "all its invocable forms" here, because a tool had two spellings and the live
    # gate matches whichever one the scheme resolved to — a deny written in the
    # unlisted spelling left the tool reachable. That machinery is deleted with the
    # second spelling: a tool has one name, so the name an operator writes IS the
    # name the gate matches.
    contextual = ContextualPermission(
        tool_allow=(
            frozenset(profile.tool_allow)
            if profile.tool_allow is not None else None
        ),
        tool_deny=frozenset(profile.tool_deny),
        mcp_allow=(
            frozenset(profile.mcp_allow) if profile.mcp_allow is not None else None
        ),
        mcp_deny=frozenset(profile.mcp_deny),
        origin=origin,
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
    applied uniformly to every axis (TOOL / MCP).
    - ``*_deny`` → union (any profile's deny wins).
    - ``*_allow`` → intersection of the *set* allow-lists (``None`` = ⊤, skipped);
      a value stays allowed only if every constraining profile allows it.
    - ``excluded_categories`` → union (any profile's hide wins).

    Empty input → an inert ``(ContextualPermission(), ∅)``.

    #3501: the flat ∩ above is what the gate evaluates, but flattening erases WHICH
    term denied a name — the information the deny message needs. So the composed
    value also carries ``composed_from``: the input terms, in composition order,
    each keeping its own ``origin``. An already-composed input contributes its own
    terms rather than itself, so the list stays one level deep and
    ``narrowing_terms`` needs no recursion.
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
    mcp_allow, mcp_deny = _compose_axis("mcp_allow", "mcp_deny")
    excluded: "set[str]" = set()
    for _c, excl in resolved:
        excluded |= set(excl)
    terms: "list[ContextualPermission]" = []
    for c in contexts:
        terms.extend(c.composed_from or (c,))
    return (
        ContextualPermission(
            tool_allow=tool_allow, tool_deny=tool_deny,
            mcp_allow=mcp_allow, mcp_deny=mcp_deny,
            composed_from=tuple(terms),
        ),
        frozenset(excluded),
    )


#: The narrowing-MAPPING keys :func:`load_capability_profile` reads as an
#: ALLOW-shaped axis — a value stays reachable only if EVERY term lists it, and an
#: ABSENT key is ⊤ (no restriction on that axis), never the empty set. ``categories``
#: belongs here and not with the denies: it is the KEEP-VISIBLE set, so intersecting
#: it is what ``compose_resolved`` expresses as "union the excluded categories".
_NARROWING_ALLOW_KEYS: "tuple[str, ...]" = ("tool_allow", "mcp_allow", "categories")

#: The narrowing-MAPPING keys read as a DENY-shaped axis — any term's deny wins, so
#: they compose by union. An absent key is the empty set.
_NARROWING_DENY_KEYS: "tuple[str, ...]" = ("tool_deny", "mcp_deny")


def compose_narrowing_mappings(
    parent: "dict | None", child: "dict | None",
) -> "dict | None":
    """Compose two RAW narrowing MAPPINGS, most-restrictive-wins (#3553).

    The mapping-level sibling of :func:`compose_resolved`, and derived from the same
    two rules rather than from a fresh judgement: **union of denials, intersection
    of allows**, applied uniformly to every axis. It exists because the sid-keyed
    #2103-S1a layer is carried between sessions as the raw ``config.yaml`` mapping
    (``AgentRegistry.per_session_narrowing`` → ``spawn_session_recorded(narrowing=)``),
    so a spawner that ALSO imposes a narrowing of its own has to combine the two
    BEFORE the child's file is written — there is no later seam where the child could
    still see its parent's mapping.

    The three cases are each uniquely determined, so composing is not a policy choice:

    - parent-only key — the child does not constrain that axis, so the parent's value
      stands (allow: ``parent ∩ ⊤ = parent``; deny: ``parent ∪ ∅ = parent``).
    - child-only key — symmetrically the child's value stands. ⚠️ This is the case an
      absent ALLOW key must NOT be read as the empty set: reading it as ∅ would make a
      childless axis deny everything, and reading the PARENT's absence as ∅ would make
      the composition unable to widen back — both are wrong for the same reason.
    - both — deny keys union, allow keys intersect.

    A key in neither table is one :func:`load_capability_profile` does not read, so it
    cannot change either session's live envelope whichever value survives; the child's
    is taken. That "cannot change" holds only while the two tables cover every axis
    field of :class:`CapabilityProfile`, which is why a test pins exactly that — a new
    axis added to the loader without being added here would start being silently
    dropped from an inherited narrowing.

    ``None`` / empty on either side returns the other unchanged (nothing to inherit,
    or nothing to impose), so the inert case stays byte-identical.
    """
    if not parent:
        return dict(child) if child else None
    if not child:
        return dict(parent)
    out: dict = dict(parent)
    for key, value in child.items():
        if key in _NARROWING_DENY_KEYS:
            base = _as_tuple(parent.get(key)) or ()
            extra = _as_tuple(value) or ()
            seen = set(base)
            out[key] = list(base) + [v for v in extra if v not in seen]
        elif key in _NARROWING_ALLOW_KEYS:
            theirs = _as_tuple(value)
            mine = _as_tuple(parent[key]) if key in parent else None
            if mine is None or theirs is None:
                # ⊤ on one side ⇒ the other side verbatim.
                out[key] = list(theirs if mine is None else mine)
            else:
                keep = set(mine)
                out[key] = [v for v in theirs if v in keep]
        else:
            out[key] = value
    return out


# ── #1827 S4: context-auto untrusted-source narrowing ───────────────────────
#
# Defense-in-depth with the #1862 content-fence: while untrusted external content
# is live in the agent's active context, the agent is also CAPABILITY-narrowed —
# so even a partial prompt-injection has no dangerous tools to reach. This is
# **seam-agnostic**: any untrusted-content seam stamps ``UNTRUSTED_META_KEY`` on
# its history/context entry meta — the external peer answer (S4 v1,
# ``intervention_handler.py``) and external tool-results (#1909,
# ``router_loop.py``'s ``feedback()``, tagged by ``returns_external_content``) both
# do — and the tainted-derivation is marker-driven, not seam-specific.

# The marker key a seam stamps on a history-entry meta to mark untrusted content.
UNTRUSTED_META_KEY: "str" = "external_source"

# The well-known auto-applied profile name. An operator
# ``.reyn/capability_profiles/_untrusted.yaml`` overrides the built-in secure
# default — an override is a *deliberate loosening*, never a tightening of the
# floor below what the operator opts into.
UNTRUSTED_PROFILE_NAME: "str" = "_untrusted"

# The reyn.yaml key that turns this narrowing on (#3501). It is OFF by default:
# a narrowing that engages without the operator asking removes capabilities
# mid-session for a reason the agent cannot see, which is a predictability cost
# paid on every session for a threat the operator may not have.
#
# Named here rather than imported from ``reyn.config`` (security must not depend
# on config) — ``tests/test_3501_untrusted_narrowing_opt_in.py`` resolves this
# dotted path against the real config objects, so a rename that misses this
# string fails rather than shipping a deny message pointing at a key that does
# not exist.
UNTRUSTED_NARROWING_CONFIG_KEY: "str" = "safety.threat_scan.capability_narrowing"

# The provenance the deny message reads (#3501). "Which narrowing / why / what
# lifts it" — the untrusted narrowing has TWO lift conditions and both must be
# stated: the taint leaving the active context (what the agent can wait out) and
# the config key (what the operator can change). Naming only the first would tell
# an operator who wants the capability back permanently nothing at all.
UNTRUSTED_NARROWING_ORIGIN: "NarrowingOrigin" = NarrowingOrigin(
    label=(
        f"the untrusted-context capability narrowing (the {UNTRUSTED_PROFILE_NAME!r} "
        "capability profile)"
    ),
    cause=(
        "content from outside this conversation is live in the active context — at "
        f"least one history entry carries the {UNTRUSTED_META_KEY!r} marker (an "
        "external peer's answer, or the result of a tool that returns external "
        "content) — so capabilities that persist, execute, install or re-delegate "
        "are withheld while it is there"
    ),
    lifts_when=(
        "that entry leaves the active context (it is compacted out), or the operator "
        f"sets `{UNTRUSTED_NARROWING_CONFIG_KEY}: off` in reyn.yaml, or loosens "
        f".reyn/capability_profiles/{UNTRUSTED_PROFILE_NAME}.yaml"
    ),
)

# The built-in secure default: deny the side-effecting / persistence /
# re-delegation / execution / install surfaces so untrusted content can be read
# and reasoned about but cannot drive irreversible actions.
#
# Grouped by CLASS (#2081 S3): the runtime FLOOR is the flat union below; the
# delegation-unsafe AUDIT (DELEGATION_AUDIT_CLASSES) derives its FLOORED classes +
# severities from these same groups — so the floor and the audit cannot drift apart.
#
# #2111 (CRITICAL fix) named each class by its QUALIFIED (universal-catalog) name
# and DERIVED the bare alias, because a tool reachable under two spellings had to be
# denied under both and the hand-written lists had missed the bare memory-write +
# mcp-install aliases — a delegate could call bare ``remember_shared`` and persist to
# shared memory. #3429 removed the second spelling, so the derivation has nothing
# left to derive: each class names the tools directly, and the gate matches those
# names on every path. Shared by BOTH floors (builtin_untrusted_profile +
# builtin_delegate_profile).
_FLOORED_TOOLS: "dict[str, frozenset[str]]" = {
    # memory writes / deletes — no persistence from untrusted content
    "memory-write": frozenset({
        "remember_shared",
        "remember_agent",
        "forget_memory",
    }),
    # re-delegation — no spawning peers from untrusted content
    "re-delegation": frozenset({"delegate_to_agent"}),
    # code execution. #3226 Phase 1: the #2593 pipeline DSL `shell` tool
    # (thin sugar over sandboxed_exec, same subprocess-exec threat surface)
    # was removed outright — it was the sole `/bin/sh -c <str>` injection
    # surface in the codebase — so it no longer needs a deny-parity entry.
    # #3226 Phase 3: the surviving tool renamed sandboxed_exec -> exec
    "exec": frozenset({"exec"}),
    # MCP install — no installing servers from untrusted content
    "mcp-install": frozenset({
        "mcp_install_registry", "mcp_install_package", "mcp_install_local",
    }),
    # skill install — no registering skills from untrusted content (mirrors mcp-install).
    # PR-D adds install_source (git/GitHub fetch) — same threat surface as install_local,
    # and higher (remote fetch adds a new HTTP trust boundary).
    "skill-install": frozenset({
        "skill_install_local",
        "skill_install_source",
    }),
    # pipeline install — no registering pipelines from untrusted content
    # (mirrors skill-install exactly: a source install adds an HTTP trust
    # boundary; a pipeline's own steps run under the launching invoker's
    # narrowed identity, but the REGISTRATION action itself must not be
    # reachable from untrusted content / an unbound delegate).
    "pipeline-install": frozenset({
        "pipeline_install_local",
        "pipeline_install_source",
    }),
    # session/agent spawn — no spawning sub-sessions/agents from untrusted content / an
    # unbound delegate (#2103: unbounded spawn is a DoS vector; the ⊆-parent model
    # blocks ESCALATION, but spawning ITSELF is restrict-floored like re-delegation —
    # default-deny, re-grantable within parent bounds by a topology binding). #2103
    # B-tool adds ``agent_spawn`` (org-design create); C1 adds ``topology_create``
    # (org-design wiring + capability-profile binding — same DoS-floor rationale: an
    # unbound delegate must not forge an org). All router-only tools with NO
    # invoke_action route today; they are floored by their own names, like
    # everything else here.
    "spawn": frozenset({"session_spawn", "agent_spawn", "topology_create"}),
    # IS-1 (pipeline v0.9 R6): no launching a registered pipeline from
    # untrusted content / an unbound delegate — a pipeline step can itself
    # write / exec / delegate (bounded ⊆ the invoker per R6, but still a
    # cost-bound multi-step dispatch), so pipeline launch gets the same
    # spawn-adjacent floor as session_spawn/agent_spawn/topology_create.
    # IS-2: the async launch (``run_pipeline_async``) is
    # the SAME threat class — it additionally spawns a driver-session, so it
    # must not be floored looser than the sync verb.
    # IS-4: the ad-hoc INLINE launches (``run_pipeline_inline`` /
    # ``run_pipeline_inline_async``) run an
    # agent-GENERATED pipeline — an even STRICTER-to-trust surface than a
    # registered one (no trusted registrant chose the steps), so they belong on
    # the SAME spawn-adjacent floor.
    "pipeline-run": frozenset({
        "run_pipeline", "run_pipeline_async",
        "run_pipeline_inline", "run_pipeline_inline_async",
    }),
}

# The floor names ARE the deny set: #3429 left every tool with exactly one
# invocable name, so there is no per-class derivation step between the declaration
# and the enforced set. ``tests/test_2111_floor_alias_completeness.py`` pins the
# replacement invariant — every floored name is a REGISTERED tool name, so a typo
# floors nothing and is caught rather than silently leaving the real route
# unguarded (the #2111 gap-class, in the opposite direction).
_FLOORED_DENY_CLASSES: "dict[str, frozenset[str]]" = dict(_FLOORED_TOOLS)
_BUILTIN_UNTRUSTED_DENY: "frozenset[str]" = frozenset().union(*_FLOORED_DENY_CLASSES.values())


def builtin_untrusted_profile() -> CapabilityProfile:
    """The built-in deny-set applied while untrusted content is live, WHEN the
    operator has opted in (``safety.threat_scan.capability_narrowing``, #3501)."""
    return CapabilityProfile(
        name=UNTRUSTED_PROFILE_NAME,
        description=(
            "applied while untrusted external content is in context, when opted into "
            "via safety.threat_scan.capability_narrowing (#1827 S4, #3501)"
        ),
        tool_deny=tuple(sorted(_BUILTIN_UNTRUSTED_DENY)),
    )


def load_untrusted_profile(project_root: "str | Path") -> CapabilityProfile:
    """The minimal profile applied while untrusted external content is live.

    Only reached when the operator opted in (#3501) — ``Session.
    _ephemeral_contextual_for_turn`` is the single gate, so this loader never runs
    at the default ``off`` setting.

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


def delegate_floor_origin(cause: str) -> "NarrowingOrigin":
    """The provenance for a term resolved to the ``_delegate`` floor (#3501).

    The floor is reached for FIVE different reasons (the default-deny policy, plus
    four fail-closed paths: a bound profile file absent, a bound profile malformed,
    a capping parent gone, a capping parent's name reused) and the reasons do not
    share a remedy — a missing file is restored, a malformed one is fixed, a lost
    parent cannot be recovered at all. So ``cause`` is per-call; the label and the
    lift condition are shared.
    """
    return NarrowingOrigin(
        label=(
            f"the delegate capability floor (the {DELEGATE_PROFILE_NAME!r} "
            "capability profile)"
        ),
        cause=cause,
        lifts_when=(
            "a topology `capability_profile` binding re-grants this agent (a binding "
            f"REPLACES the floor), or .reyn/capability_profiles/{DELEGATE_PROFILE_NAME}"
            ".yaml is loosened. Note a re-grant is still capped at the spawning "
            "agent's own surface"
        ),
    )


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
    "skill-install": "HIGH",  # #2548: registering skills from untrusted content is a persistence vector
    "pipeline-install": "HIGH",  # registering pipelines from untrusted content — same persistence vector as skill-install
    "memory-write": "MED",
    "spawn": "HIGH",  # #2103: unbounded sub-session spawn (DoS) — peer of re-delegation
    "pipeline-run": "HIGH",  # IS-1: pipeline launch is spawn-adjacent (peer of "spawn")
}
DELEGATION_AUDIT_CLASSES: "dict[str, tuple[str, frozenset[str]]]" = {
    cls: (_FLOORED_AUDIT_SEVERITY[cls], tools)
    for cls, tools in _FLOORED_DENY_CLASSES.items()
}
DELEGATION_AUDIT_CLASSES["destructive-fs"] = ("MED", frozenset({"delete_file"}))


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
