"""reyn.core.part_type_registry — the part-type meta-registry SSoT (proposal
``docs/deep-dives/proposals/0060-llm-wielding-foundation.md`` §2.4/§3 F1).

Skills / pipelines / MCP servers / hooks / presentations each have their own
separate registry (``reyn.data.skills.registry``,
``reyn.data.pipelines.registry``, the MCP server roster on
``RouterHostAdapter.get_mcp_servers``, ``reyn.hooks.loader.load_hooks``,
``reyn.data.presentations.registry``) with no unified enumeration — no single
place lists "every kind of part reyn has" with the input/workflow/output
role(s) it plays (§2.1) and the catalog category it belongs to. This module
is that single place, and — like ``OP_KIND_MODEL_MAP`` /
``BUILTIN_HOOK_SCHEMAS`` — it is DERIVED, never a hand-maintained list.

**Discovery, not a hand-list (the completeness property).** The map is BUILT
by walking the ``reyn.core.part_types`` package: every submodule that exports
a ``PART_TYPE_SPEC`` module-level marker is collected; a submodule without the
marker is structurally not a part-type. Adding a new part-type is therefore
"drop a marked module into ``reyn/core/part_types/``, touch nothing else" —
it auto-appears in ``PART_TYPE_REGISTRY``. There is no central list to forget
to update (the marker-in-the-module IS the registration), so forgotten-
registration silent drift is impossible by construction.

``registry_ref`` on each spec is a ``"module:qualname"`` pointer at the REAL
per-part-type registry/builder that part-type's live instances come from. It
is LIVE-RESOLVED at collection time (:func:`resolve_registry_ref`) — a stale /
typo'd ref (a renamed or deleted builder) fails the build loudly, never
passes as dead documentation.

Three declared consumers (proposal §3 F1, none built in this phase — this
module only has to make them possible): the taxonomy CI gate walks the map and
fails on any uncatalogued part-type; a future builtin-tier (F3) populates
catalog content per part-type; the live action catalog
(``reyn.tools.universal_catalog``) can read it to keep its category set in
sync. Wiring those consumers is follow-up work, not this module's shape.

``task`` is deliberately excluded (§2.1: "task is excluded — deprecating").
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from types import ModuleType

# The three roles a part can play (proposal §2.1 — not exclusive bins; a part
# may play more than one, e.g. ``mcp`` plays all three).
PART_ROLES: tuple[str, ...] = ("input", "workflow", "output")

# The module-level attribute a part-type module MUST export to be collected.
# A module in the part_types package without this attribute is, by definition,
# not a part-type (an explicit non-membership, not silent drift).
MARKER_ATTR: str = "PART_TYPE_SPEC"


@dataclass(frozen=True)
class PartTypeSpec:
    """One part-type's declaration — the value a ``PART_TYPE_SPEC`` marker holds.

    ``roles`` — subset of :data:`PART_ROLES` this part-type plays (§2.1).
    ``category`` — the catalog/taxonomy category label this part-type is filed
        under. Required non-empty (the taxonomy gate's invariant).
    ``registry_ref`` — ``"module:qualname"`` pointing at the REAL
        per-part-type registry/builder this part-type's live instances are
        enumerated from. Live-resolved at collection time via
        :func:`resolve_registry_ref`; a ref that does not resolve fails the
        build (never dead documentation).
    ``description`` — one-line human-facing summary of what this part-type is.
    """

    name: str
    roles: frozenset[str]
    category: str
    registry_ref: str
    description: str

    def __post_init__(self) -> None:
        unknown = set(self.roles) - set(PART_ROLES)
        if unknown:
            raise ValueError(
                f"part-type {self.name!r} declares unknown role(s) {sorted(unknown)}; "
                f"expected a subset of {PART_ROLES}"
            )


class PartTypeCollectionError(RuntimeError):
    """A part-type marker could not be collected — a malformed ``PART_TYPE_SPEC``,
    a duplicate part-type name across two marker modules, or a ``registry_ref``
    that does not resolve to a real object. Fail-loud: a broken part-type
    declaration must surface at build time, not ship as a silent gap."""


def resolve_registry_ref(registry_ref: str) -> object:
    """Import and return the object a ``"module:qualname"`` ref names.

    ``qualname`` may be dotted (``"RouterHostAdapter.get_mcp_servers"``) — each
    segment is resolved by attribute walk. Raises
    :class:`PartTypeCollectionError` if the ref is malformed, the module cannot
    be imported, or any attribute in the chain is missing (this is what makes
    ``registry_ref`` a LIVE field, not a shape-only string)."""
    if ":" not in registry_ref:
        raise PartTypeCollectionError(
            f"registry_ref {registry_ref!r} is not 'module:qualname' shaped"
        )
    module_path, qualname = registry_ref.split(":", 1)
    if not module_path or not qualname:
        raise PartTypeCollectionError(
            f"registry_ref {registry_ref!r} has an empty module or qualname"
        )
    try:
        obj: object = importlib.import_module(module_path)
    except ImportError as exc:
        raise PartTypeCollectionError(
            f"registry_ref {registry_ref!r}: cannot import module {module_path!r}: {exc}"
        ) from exc
    for attr in qualname.split("."):
        try:
            obj = getattr(obj, attr)
        except AttributeError as exc:
            raise PartTypeCollectionError(
                f"registry_ref {registry_ref!r}: {qualname!r} does not resolve "
                f"(missing attribute {attr!r})"
            ) from exc
    return obj


def collect_part_types(package: ModuleType) -> dict[str, PartTypeSpec]:
    """Build a ``name -> PartTypeSpec`` map by DISCOVERING marker modules.

    Walks *package*'s submodules (``pkgutil.iter_modules`` over its
    ``__path__``), imports each, and collects the ``PART_TYPE_SPEC`` marker
    from every submodule that exports one. Submodules without the marker are
    skipped (not part-types). No hardcoded submodule list — a newly-added
    marked module in *package* is picked up with no edit here.

    Each collected spec's ``registry_ref`` is LIVE-RESOLVED (a stale ref fails
    the build). A duplicate part-type name or a non-``PartTypeSpec`` marker
    raises :class:`PartTypeCollectionError`."""
    collected: dict[str, PartTypeSpec] = {}
    for mod_info in pkgutil.iter_modules(package.__path__):
        submodule = importlib.import_module(f"{package.__name__}.{mod_info.name}")
        spec = getattr(submodule, MARKER_ATTR, None)
        if spec is None:
            continue  # not a part-type module
        if not isinstance(spec, PartTypeSpec):
            raise PartTypeCollectionError(
                f"{submodule.__name__}.{MARKER_ATTR} is {type(spec).__name__}, "
                f"not a PartTypeSpec"
            )
        if spec.name in collected:
            raise PartTypeCollectionError(
                f"duplicate part-type name {spec.name!r} "
                f"(second declaration in {submodule.__name__})"
            )
        # LIVE-RESOLVE registry_ref — a stale/typo'd ref fails the build here.
        resolve_registry_ref(spec.registry_ref)
        collected[spec.name] = spec
    return collected


def build_part_type_registry() -> dict[str, PartTypeSpec]:
    """Collect the part-type map from the ``reyn.core.part_types`` package.

    Imported lazily to keep the ``reyn.core.part_types`` ↔ this-module import
    order acyclic (each marker module imports :class:`PartTypeSpec` from here,
    so this module's names must be defined before the package is walked)."""
    from reyn.core import part_types

    return collect_part_types(part_types)


# ── The meta-registry itself (derived) ──────────────────────────────────────
PART_TYPE_REGISTRY: dict[str, PartTypeSpec] = build_part_type_registry()


def all_part_types() -> tuple[str, ...]:
    """Every registered part-type name (discovery order)."""
    return tuple(PART_TYPE_REGISTRY)


def part_types_for_role(role: str) -> tuple[str, ...]:
    """Part-type names that play ``role`` (one of :data:`PART_ROLES`)."""
    if role not in PART_ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {PART_ROLES}")
    return tuple(
        name for name, spec in PART_TYPE_REGISTRY.items() if role in spec.roles
    )
