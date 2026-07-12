"""reyn.core.part_type_registry — the part-type meta-registry (proposal
``docs/deep-dives/proposals/0060-llm-wielding-foundation.md`` §2.4/§3 F1).

Today skills / pipelines / MCP servers / hooks / presentations each have their
own separate registry (``reyn.data.skills.registry``,
``reyn.data.pipelines.registry``, the MCP server roster on
``RouterHostAdapter.get_mcp_servers``, ``reyn.hooks.loader.load_hooks``,
``reyn.data.presentations.registry``) with no unified enumeration — there is
no single place that lists "every kind of part reyn has" alongside the
input/workflow/output role(s) it plays (proposal §2.1) or the catalog
category it belongs to. This module is that single place.

Mirrors the ``OP_KIND_MODEL_MAP`` (``reyn.schemas.models``) /
``BUILTIN_HOOK_SCHEMAS`` (``reyn.hooks.schema_registry``) pattern: a plain
code-shipped ``dict`` literal that other things DERIVE from, never a
hand-curated duplicate list kept in sync by hand. ``PART_TYPE_REGISTRY``
below is that dict; ``registry_ref`` on each entry POINTS AT the real
per-part-type registry (a dotted ``module:qualname`` string) rather than
importing/calling it eagerly — every real registry needs different
construction arguments (a config dict, a project root, ...), so the
meta-registry stays a lightweight index of WHERE the real registry lives, not
a re-implementation of it.

Three declared consumers (proposal §3 F1, none built here — this module only
has to make them possible):

1. the taxonomy CI gate (``tests/test_part_type_registry_taxonomy_0060.py``)
   walks this dict and fails if any entry lacks a non-empty ``category`` —
   registry-derived completeness, so a new part-type with no assigned
   category turns the gate red instead of silently shipping uncatalogued.
2. a future builtin-tier (proposal F3, NOT built in this phase) populates
   catalog content per part-type using this as its part-type index.
3. the live action catalog (``reyn.tools.universal_catalog``) can eventually
   read this to keep its category set in sync with the part-type set — NOT
   done in this phase (this module does not touch ``CATEGORIES``; see the
   module-level note in that file). Wiring the two together is a follow-up
   fork, not part of the meta-registry's own shape.

``task`` is deliberately excluded (proposal §2.1: "task is excluded —
deprecating").
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The three roles a part can play (proposal §2.1 — not exclusive bins; a part
# may play more than one, e.g. ``mcp`` plays all three).
PART_ROLES: tuple[str, ...] = ("input", "workflow", "output")


@dataclass(frozen=True)
class PartTypeSpec:
    """One row of the part-type meta-registry.

    ``roles`` — subset of ``PART_ROLES`` this part-type plays (§2.1).
    ``category`` — the catalog/taxonomy category label this part-type is
        filed under. Required non-empty (the taxonomy gate's invariant).
    ``registry_ref`` — ``"module:qualname"`` pointing at the REAL
        per-part-type registry/loader this part-type's live instances are
        enumerated from. Documentation + gate-walk target only; the
        meta-registry never imports or calls it, so adding a part-type here
        never requires that its real registry be import-safe at meta-registry
        import time.
    ``description`` — one-line human-facing summary of what this part-type is.
    """

    name: str
    roles: frozenset[str]
    category: str
    registry_ref: str
    description: str


def _spec(
    name: str, roles: tuple[str, ...], category: str, registry_ref: str, description: str
) -> PartTypeSpec:
    unknown = set(roles) - set(PART_ROLES)
    if unknown:
        raise ValueError(f"part-type {name!r} declares unknown role(s) {sorted(unknown)}")
    return PartTypeSpec(
        name=name,
        roles=frozenset(roles),
        category=category,
        registry_ref=registry_ref,
        description=description,
    )


# ── The meta-registry itself ────────────────────────────────────────────────
PART_TYPE_REGISTRY: dict[str, PartTypeSpec] = {
    "skill": _spec(
        "skill",
        roles=("workflow",),
        category="skill_management",
        registry_ref="reyn.data.skills.registry:build_skill_registry",
        description="A named SKILL.md instruction set, read by the model at L2.",
    ),
    "pipeline": _spec(
        "pipeline",
        roles=("workflow",),
        category="pipeline_management",
        registry_ref="reyn.data.pipelines.registry:build_pipeline_registry",
        description="A registered multi-step orchestration DSL document.",
    ),
    "mcp": _spec(
        "mcp",
        roles=("input", "workflow", "output"),
        category="mcp",
        registry_ref=(
            "reyn.runtime.services.router_host_adapter:RouterHostAdapter.get_mcp_servers"
        ),
        description="An installed external MCP server (tools/resources/prompts).",
    ),
    "hook": _spec(
        "hook",
        roles=("input",),
        category="hook",
        registry_ref="reyn.hooks.loader:load_hooks",
        description="A reactive hook-event registration (trigger + action glue).",
    ),
    "presentation": _spec(
        "presentation",
        roles=("output",),
        category="presentation",
        registry_ref="reyn.data.presentations.registry:build_presentation_registry",
        description="A named, operator-facing presentation/render template.",
    ),
}


def all_part_types() -> tuple[str, ...]:
    """Every registered part-type name, in ``PART_TYPE_REGISTRY`` declaration order."""
    return tuple(PART_TYPE_REGISTRY)


def part_types_for_role(role: str) -> tuple[str, ...]:
    """Part-type names that play ``role`` (one of ``PART_ROLES``), declaration order."""
    if role not in PART_ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {PART_ROLES}")
    return tuple(name for name, spec in PART_TYPE_REGISTRY.items() if role in spec.roles)
