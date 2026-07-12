"""Presentation verb-object handler — install (proposal 0060 Phase 1 Layer A, A8).

Router-callable presentation management verb under the ``presentation_management``
category:

  - ``presentation_management__install`` — register a named presentation
    template (a declarative component tree) into the project
    ``.reyn/config/presentations.yaml``, making it available to sessions that
    load the config cascade.

Mirrors ``skill_verbs.py`` / ``pipeline_management_verbs.py`` STRUCTURE (a
single delegating handler + ``ToolDefinition``), but there is only ONE verb —
unlike skill/pipeline, a presentation blueprint is small declarative data
carried inline (never a file-backed artifact), so there is no
``install_source`` git-fetch counterpart.

NOTE: there is no ``presentation__`` resource-category prefix today (present
templates are resolved via ``present(view=<name>)``, not a per-template
dynamic-dispatch verb) — so ``presentation_management__`` is namespaced purely
for consistency with ``skill_management__`` / ``pipeline_management__``, not to
avoid an actual collision.

Delegates to ``op_runtime/presentation_install.py`` via the
``build_legacy_op_context`` bridge (same pattern as the skill/pipeline/mcp
install verbs).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import presentation_management as _presentation_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── presentation_management__install ─────────────────────────────────────────

_PRESENTATION_INSTALL_DESCRIPTION = _presentation_descriptions.presentation_install.text

_PRESENTATION_INSTALL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                _presentation_descriptions.PARAMS["presentation_install_local"]["name"].text
            ),
        },
        "blueprint": {
            "description": (
                _presentation_descriptions.PARAMS["presentation_install_local"]["blueprint"].text
            ),
        },
    },
    "required": ["name", "blueprint"],
}


async def _handle_presentation_install(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Register a named presentation template by writing .reyn/config/presentations.yaml.

    Delegates to op_runtime/presentation_install.handle via
    build_legacy_op_context (same bridge pattern as skill/pipeline/mcp-install
    verbs). The handler structurally validates the blueprint
    (validate_blueprint — the threat gate, A8), gates the config write,
    writes the entry with an OS-stamped provenance (A9), records a config
    generation for crash-recovery, emits a presentation_installed event, and
    requests a hot-reload.
    """
    from reyn.core.op_runtime.presentation_install import (
        handle as presentation_install_handle,
    )
    from reyn.schemas.models import PresentationInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.tools.op_context_bridge import build_legacy_op_context

    name = str(args.get("name") or "").strip()
    if not name:
        return {
            "status": "error",
            "data": {"error": "name is required"},
        }

    blueprint = args.get("blueprint")
    if blueprint is None:
        return {
            "status": "error",
            "data": {"error": "blueprint is required"},
        }

    try:
        op = PresentationInstallIROp(
            kind="presentation_install",
            name=name,
            blueprint=blueprint,
        )
    except Exception as exc:
        return {
            "status": "error",
            "data": {"error": f"invalid args: {exc}"},
        }

    decl = PermissionDecl()
    decl.file_write = [{"path": ".reyn/config/presentations.yaml"}]

    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.actor = "presentation_management__install"

    result = await presentation_install_handle(op, op_ctx)
    return {"status": "ok", "data": result}


# ── ToolDefinition ────────────────────────────────────────────────────────────

from reyn.core.offload.canonical import (  # noqa: E402
    presentation_install_verb_to_canonical,
)

PRESENTATION_INSTALL = ToolDefinition(
    canonical=presentation_install_verb_to_canonical,
    # NOTE: named "presentation_install_local" (not the bare "presentation_install"
    # op kind) — the tool-name and op-kind identities are separate
    # declare_canonical source_ids (mirrors skill_install_local/skill_install vs
    # pipeline_install_local/pipeline_install); reusing the op-kind's bare name
    # here collides its STRUCTURED_PASSTHROUGH op-runtime declaration with this
    # tool's own mapper declaration under the SAME source_id.
    name="presentation_install_local",
    description=_PRESENTATION_INSTALL_DESCRIPTION,
    parameters=_PRESENTATION_INSTALL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_presentation_install,
    category="io",
    purity="side_effect",
    # proposal 0060 D5d: mirrors the "presentation" PartTypeSpec's doc_ref
    # (reyn.core.part_types.presentation) — same part-type, install-verb axis.
    doc_ref="docs/concepts/runtime/present.md",
)

__all__ = ["PRESENTATION_INSTALL"]
