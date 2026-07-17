"""Pipeline verb-object handlers — local install + source/git install.

Router-callable pipeline management verbs under the ``pipeline_management``
category. Mirrors ``skill_verbs.py`` as closely as possible. Exposes two
install verbs:

  - ``pipeline_management__install_local`` — register a local pipeline DSL
    file into the project ``.reyn/config/pipelines.yaml``, making it available
    to sessions that load the config cascade.

  - ``pipeline_management__install_source`` — fetch a pipeline from a
    git/GitHub URL, install it into ``.reyn/pipelines/<name>/``, and register
    the installed copy. Requires a ``require_http_get`` gate for the source
    host + the ``require_file_write`` gate for pipelines.yaml.

NOTE: ``pipeline__`` is the LAUNCH category (``pipeline__run`` / ``__list`` /
the async + inline variants). Management operations use
``pipeline_management__`` to keep the two planes apart. ``pipeline__<name>``
(e.g. ``pipeline__hello``) still RESOLVES as an author-time name — it is the
form the user guide teaches — but #3026 stopped ENUMERATING it: one action per
registered pipeline made the LLM's tools= payload scale with the operator's
pipelines. (The old wording here called that a "resource namespace" mirroring
``skill__``; there is no ``skill__`` category and never has been — see
``universal_catalog``'s module docstring on how that phantom misled #1647.)

Both verbs delegate to ``op_runtime/pipeline_install.py`` via the
``build_legacy_op_context`` bridge (same pattern as the skill/mcp-install
verbs).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import pipeline_management as _pipeline_management_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── pipeline_management__install_local ───────────────────────────────────────

# Relocated to reyn.tools.descriptions.pipeline_management (Phase 3
# tool-description package refactor — byte-identical, no LLM-facing text
# change).
_PIPELINE_INSTALL_LOCAL_DESCRIPTION = (
    _pipeline_management_descriptions.pipeline_install_local.text
)

_PIPELINE_INSTALL_LOCAL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                _pipeline_management_descriptions.PARAMS["pipeline_install_local"]["path"].text
            ),
        },
        "name": {
            "type": "string",
            "description": (
                _pipeline_management_descriptions.PARAMS["pipeline_install_local"]["name"].text
            ),
        },
    },
    "required": ["path"],
}


async def _handle_pipeline_install_local(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Register a local pipeline DSL file by writing .reyn/config/pipelines.yaml.

    Delegates to op_runtime/pipeline_install.handle via build_legacy_op_context
    (same bridge pattern as skill_management__install_local). The handler
    parses the DSL, validates the name, threat-scans the description, gates
    the config write, writes the entry, records a config generation for
    crash-recovery, emits a pipeline_installed event, and requests a
    hot-reload.
    """
    from reyn.core.op_runtime.pipeline_install import handle as pipeline_install_handle
    from reyn.schemas.models import PipelineInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.tools.op_context_bridge import build_legacy_op_context

    path = str(args.get("path") or "").strip()
    if not path:
        return {
            "status": "error",
            "data": {"error": "path is required"},
        }

    raw_name = args.get("name")
    name_override = str(raw_name).strip() if raw_name else None

    try:
        op = PipelineInstallIROp(
            kind="pipeline_install",
            path=path,
            name=name_override,
        )
    except Exception as exc:
        return {
            "status": "error",
            "data": {"error": f"invalid args: {exc}"},
        }

    decl = PermissionDecl()
    decl.file_write = [{"path": ".reyn/config/pipelines.yaml"}]

    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.actor = "pipeline_management__install_local"

    result = await pipeline_install_handle(op, op_ctx)
    return {"status": "ok", "data": result}


# ── pipeline_management__install_source ──────────────────────────────────────

# Relocated to reyn.tools.descriptions.pipeline_management (Phase 3
# tool-description package refactor — byte-identical, no LLM-facing text
# change).
_PIPELINE_INSTALL_SOURCE_DESCRIPTION = (
    _pipeline_management_descriptions.pipeline_install_source.text
)

_PIPELINE_INSTALL_SOURCE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": (
                _pipeline_management_descriptions.PARAMS["pipeline_install_source"]["source"].text
            ),
        },
        "path": {
            "type": "string",
            "description": (
                _pipeline_management_descriptions.PARAMS["pipeline_install_source"]["path"].text
            ),
        },
        "name": {
            "type": "string",
            "description": (
                _pipeline_management_descriptions.PARAMS["pipeline_install_source"]["name"].text
            ),
        },
    },
    "required": ["source"],
}


async def _handle_pipeline_install_source(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Fetch and install a pipeline from a git/GitHub URL.

    Delegates to op_runtime/pipeline_install.handle via build_legacy_op_context
    (same bridge pattern as skill_management__install_source). The handler:
      1. Gates require_http_get for the source host.
      2. Shallow-clones the repo to .reyn/pipelines/<name>/.
      3. Locates + parses the DSL file from the clone (root or subdir via //;
         'path' selects it when ambiguous).
      4. Resolves the namespace key (#2722: 'name' or the source basename;
         every pipeline registers as '<key>.<declared-name>').
      5. Threat-scans the description (scope=strict; block on blocking match).
      6. Gates require_file_write for .reyn/config/pipelines.yaml.
      7. Writes the pipelines.yaml entry with the installed clone path + source URL.
      8. Records a config generation for crash-recovery.
      9. Emits pipeline_installed event and requests a hot-reload.
    """
    from reyn.core.op_runtime.pipeline_install import handle as pipeline_install_handle
    from reyn.schemas.models import PipelineInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.tools.op_context_bridge import build_legacy_op_context

    source = str(args.get("source") or "").strip()
    if not source:
        return {
            "status": "error",
            "data": {"error": "source is required"},
        }

    raw_name = args.get("name")
    name_override = str(raw_name).strip() if raw_name else None
    raw_path = args.get("path")
    path_override = str(raw_path).strip() if raw_path else ""

    try:
        op = PipelineInstallIROp(
            kind="pipeline_install",
            source=source,
            path=path_override,
            name=name_override,
        )
    except Exception as exc:
        return {
            "status": "error",
            "data": {"error": f"invalid args: {exc}"},
        }

    decl = PermissionDecl()
    decl.file_write = [{"path": ".reyn/config/pipelines.yaml"}]
    # Declare http.get with wildcard — the source host is determined at call time.
    # The resolver gates the actual host via the wildcard path (= JIT prompt).
    decl.http_get = [{"host": "*"}]

    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.actor = "pipeline_management__install_source"

    result = await pipeline_install_handle(op, op_ctx)
    return {"status": "ok", "data": result}


# ── ToolDefinitions ───────────────────────────────────────────────────────────

from reyn.core.offload.canonical import pipeline_install_verb_to_canonical  # noqa: E402

# proposal 0060 D5d: mirrors the "pipeline" PartTypeSpec's doc_ref
# (reyn.core.part_types.pipeline) — same part-type, install-verb axis.
_PIPELINE_DOC_REF = "docs/reference/runtime/pipeline-dsl.md"

PIPELINE_INSTALL_LOCAL = ToolDefinition(
    canonical=pipeline_install_verb_to_canonical,
    name="pipeline_install_local",
    description=_PIPELINE_INSTALL_LOCAL_DESCRIPTION,
    parameters=_PIPELINE_INSTALL_LOCAL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_pipeline_install_local,
    category="io",
    purity="side_effect",
    doc_ref=_PIPELINE_DOC_REF,
)

PIPELINE_INSTALL_SOURCE = ToolDefinition(
    canonical=pipeline_install_verb_to_canonical,
    name="pipeline_install_source",
    description=_PIPELINE_INSTALL_SOURCE_DESCRIPTION,
    parameters=_PIPELINE_INSTALL_SOURCE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_pipeline_install_source,
    category="io",
    purity="side_effect",
    doc_ref=_PIPELINE_DOC_REF,
)

__all__ = ["PIPELINE_INSTALL_LOCAL", "PIPELINE_INSTALL_SOURCE"]
