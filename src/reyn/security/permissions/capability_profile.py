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
