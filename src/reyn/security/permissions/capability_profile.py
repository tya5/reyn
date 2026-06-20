"""Capability profile (#1827 S2a) — the named role spec + its pure resolver.

A ``capability_profile`` is a named, declarative narrowing of an agent's
capabilities, loaded from ``.reyn/capability_profiles/<name>.yaml``. It is the
single source for BOTH #1827 axes:

- **authority (enforcement, axis A)** → a :class:`ContextualPermission`
  (``tool_allow`` / ``tool_deny``) that rides the live ∩-gate (S1.5, #1884).
- **visibility (cognitive, axis B)** → an ``excluded_categories`` set derived
  from ``categories`` against the canonical 12-entry catalog.

This module is PURE — schema + loader + resolver + compose. It is **unwired**:
no binding to topology / delegate / ephemeral yet (S2b/S3 wire it through the
``scoped_session_factory`` #1402 single-source). With no profile applied the
session is byte-identical to pre-#1827.

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

    - ``categories`` — the catalog categories to KEEP VISIBLE (axis B). ``None``
      = no visibility narrowing (show everything). An explicit (possibly empty)
      tuple narrows the view to that set.
    - ``tool_allow`` — the tools to permit (axis A allow-list). ``None`` =
      unconstrained (deny-list only).
    - ``tool_deny`` — the tools to forbid (axis A).

    Tuples (not sets) so the dataclass stays frozen/hashable; the resolver
    converts to frozensets.
    """

    name: str
    description: str = ""
    categories: "tuple[str, ...] | None" = None
    tool_allow: "tuple[str, ...] | None" = None
    tool_deny: "tuple[str, ...]" = ()


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
    )


def resolve_profile(
    profile: CapabilityProfile,
) -> "tuple[ContextualPermission, frozenset[str]]":
    """Resolve a profile into ``(ContextualPermission, excluded_categories)``.

    - enforcement: ``ContextualPermission(tool_allow, tool_deny)`` — the S1 ∩ term.
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

    Monotonic with the S1 ∩ model: **union of denials, intersection of allows**.
    - ``tool_deny`` → union (any profile's deny wins).
    - ``tool_allow`` → intersection of the *set* allow-lists (``None`` = ⊤, skipped);
      a tool stays allowed only if every constraining profile allows it.
    - ``excluded_categories`` → union (any profile's hide wins).

    Empty input → an inert ``(ContextualPermission(), ∅)``.
    """
    deny: "set[str]" = set()
    excluded: "set[str]" = set()
    allow_sets: "list[frozenset[str]]" = []
    for contextual, excl in resolved:
        deny |= set(contextual.tool_deny)
        excluded |= set(excl)
        if contextual.tool_allow is not None:
            allow_sets.append(contextual.tool_allow)
    if allow_sets:
        combined_allow: "frozenset[str] | None" = frozenset.intersection(*allow_sets)
    else:
        combined_allow = None
    return (
        ContextualPermission(tool_allow=combined_allow, tool_deny=frozenset(deny)),
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
_BUILTIN_UNTRUSTED_DENY: "frozenset[str]" = frozenset({
    # memory writes / deletes — no persistence from untrusted content
    "memory_operation__remember_shared",
    "memory_operation__remember_agent",
    "memory_operation__forget",
    # re-delegation — no spawning peers from untrusted content
    "multi_agent__delegate", "delegate_to_agent",
    # code execution
    "exec__sandboxed_exec", "sandboxed_exec",
    # MCP install — no installing servers from untrusted content
    "mcp__install_registry", "mcp__install_package", "mcp__install_local",
})


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
