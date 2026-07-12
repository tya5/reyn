"""Tier 1: contract — proposal 0060 Addendum D, D5d (`doc_ref` first-class field
+ registry-walk completeness gate).

Co-vet pins:

  1. **Registry-walk completeness (PART_TYPE_REGISTRY).** Every part-type
     collected by :func:`reyn.core.part_type_registry.build_part_type_registry`
     declares a non-empty ``doc_ref`` — a real ``docs/...`` path or the
     explicit ``DOC_REF_NONE`` sentinel. Enumerated from the LIVE registry
     (``pkgutil``-discovered marker modules under ``reyn.core.part_types``),
     never a hand list — a newly added part-type is walked automatically.
  2. **Falsify: omitting doc_ref is a hard construction error**, not a silent
     gap — ``PartTypeSpec`` has no default for the field, so a marker module
     that forgets it fails at import time (caught here by constructing one
     directly without the kwarg).
  3. **Falsify: an empty-string doc_ref is rejected** (only a real path or the
     explicit sentinel is a valid declaration — an accidental ``""`` is not
     "explicitly declared none").
  4. **ToolDefinition doc_ref surfaces the same set the D2 reachability audit
     named** (skill/pipeline/mcp/present/render_template/hook installs) — each
     of those ToolDefinitions is checked directly (a small, closed, named set —
     see module docstring in each ``tools/*.py`` for the mirrored PartTypeSpec
     it points at).
"""
from __future__ import annotations

import pytest

from reyn.core.part_type_registry import DOC_REF_NONE, PART_TYPE_REGISTRY, PartTypeSpec


@pytest.mark.parametrize("name", sorted(PART_TYPE_REGISTRY))
def test_every_part_type_declares_a_doc_ref(name: str) -> None:
    """Tier 1: every registered part-type has a non-empty doc_ref — a real
    docs/... path or the explicit DOC_REF_NONE sentinel."""
    spec = PART_TYPE_REGISTRY[name]
    assert spec.doc_ref, f"part-type {name!r} has an empty doc_ref"
    assert spec.doc_ref == DOC_REF_NONE or spec.doc_ref.startswith("docs/"), (
        f"part-type {name!r}: doc_ref {spec.doc_ref!r} is neither a docs/... "
        f"path nor the {DOC_REF_NONE!r} sentinel"
    )


def test_part_type_registry_is_the_five_expected_part_types() -> None:
    """Tier 1: (regrounding) the registry this gate walks is the real, live,
    discovered set — not a hand list this test invented. Guards against the
    gate silently walking zero entries (which would vacuously "pass")."""
    assert set(PART_TYPE_REGISTRY) == {"skill", "pipeline", "mcp", "hook", "presentation"}


def test_falsify_missing_doc_ref_is_a_construction_error() -> None:
    """Tier 1: (falsify) doc_ref has NO default — a marker module that omits
    it fails at PartTypeSpec construction time, not silently."""
    with pytest.raises(TypeError):
        PartTypeSpec(  # type: ignore[call-arg]
            name="fixture_missing_doc_ref",
            roles=frozenset({"workflow"}),
            category="test",
            registry_ref="reyn.data.skills.registry:build_skill_registry",
            description="a fixture part-type that forgot doc_ref",
        )


def test_falsify_empty_string_doc_ref_is_rejected() -> None:
    """Tier 1: (falsify) an empty-string doc_ref is not treated as "explicitly
    declared none" — only the real DOC_REF_NONE sentinel or a real path counts."""
    with pytest.raises(ValueError):
        PartTypeSpec(
            name="fixture_empty_doc_ref",
            roles=frozenset({"workflow"}),
            category="test",
            registry_ref="reyn.data.skills.registry:build_skill_registry",
            description="a fixture part-type with an empty doc_ref",
            doc_ref="",
        )


# ── ToolDefinition side: the D2-audited spec-bearing set ────────────────────
#
# The Addendum D reachability audit (D2) named 7 rows; 5 are PartTypeSpec
# part-types (walked above) and 2 are ops with no installable part-type
# (present / render_template). This is a small, closed, explicitly-justified
# set (not a hand-list-drift risk — it mirrors the design doc's own finite
# audit table), distinct from the open-ended ToolRegistry walk FP-0056's
# canonical-coverage gate performs over EVERY tool (doc_ref is not required
# on every tool — most tools are ordinary file/task verbs a base model
# already knows how to call).
_D2_AUDITED_TOOL_DEFINITIONS = (
    ("reyn.tools.present", "PRESENT"),
    ("reyn.tools.render_template", "RENDER_TEMPLATE"),
    ("reyn.tools.hooks", "HOOKS_ADD"),
    ("reyn.tools.skill_verbs", "SKILL_INSTALL_LOCAL"),
    ("reyn.tools.skill_verbs", "SKILL_INSTALL_SOURCE"),
    ("reyn.tools.pipeline_management_verbs", "PIPELINE_INSTALL_LOCAL"),
    ("reyn.tools.pipeline_management_verbs", "PIPELINE_INSTALL_SOURCE"),
    ("reyn.tools.mcp_install", "MCP_INSTALL_OP"),
    ("reyn.tools.presentation_management_verbs", "PRESENTATION_INSTALL"),
)


@pytest.mark.parametrize(
    "module_path, attr_name", _D2_AUDITED_TOOL_DEFINITIONS,
    ids=[f"{m}.{a}" for m, a in _D2_AUDITED_TOOL_DEFINITIONS],
)
def test_d2_audited_tool_definition_declares_doc_ref(module_path: str, attr_name: str) -> None:
    """Tier 1: every ToolDefinition the D2 reachability audit named as
    spec-bearing carries a non-empty, docs/...-shaped doc_ref."""
    import importlib

    tool = getattr(importlib.import_module(module_path), attr_name)
    assert tool.doc_ref, f"{module_path}.{attr_name}: doc_ref is unset"
    assert tool.doc_ref.startswith("docs/"), (
        f"{module_path}.{attr_name}: doc_ref {tool.doc_ref!r} is not docs/...-shaped"
    )


def test_falsify_tool_definition_without_doc_ref_defaults_to_none() -> None:
    """Tier 1: (falsify) ToolDefinition.doc_ref defaults to None when a
    construction site omits it — proving the field is a real, checkable
    signal (not a value every ToolDefinition gets "for free")."""
    from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

    async def _noop(args, ctx: ToolContext) -> ToolResult:  # pragma: no cover
        return {}

    fixture = ToolDefinition(
        name="fixture_no_doc_ref",
        description="a tool that never declared doc_ref",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(),
        handler=_noop,
        category="test",
    )
    assert fixture.doc_ref is None
