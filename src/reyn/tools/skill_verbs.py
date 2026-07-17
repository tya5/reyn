"""Skill verb-object handlers — local install (#2548 PR-C) + source/git install
(#2548 PR-D) + discovery (#2971).

Router-callable skill management verbs under the ``skill_management`` category.
Exposes two install verbs and one discovery verb:

  - ``skill_management__install_local`` — register a local skill directory
    (one containing a ``SKILL.md`` file) into the project
    ``.reyn/config/skills.yaml``, making it available to sessions
    that load the config cascade.

  - ``skill_management__install_source`` — fetch a skill from a git/GitHub URL,
    install it into ``.reyn/skills/<name>/``, and register the installed copy.
    Requires a ``require_http_get`` gate for the source host + the
    ``require_file_write`` gate for skills.yaml.

  - ``skill_management__list`` — return every registered skill whose
    ``visibility`` is not ``hidden`` (name / description / path). Read-only,
    no permission gate: it reveals only the operator's own declarations, and
    strictly less than the L1 Skills menu already puts in the system prompt.

**Why there is a list verb but no run verb (#2971).** Until #2971 the
``skill_management`` category was install-only, and the L1 Skills menu was the
only surface that named a skill — so a skill the menu excluded could not be
reached by the model, the operator, or anything else. Registering it did
nothing. The fix needs exactly one new hop, DISCOVERY, because the invocation
path already exists and is complete: the menu has always shipped each skill's
``path``, and "invoking" a skill means reading that file with the ordinary
``file`` read op and following its instructions — a skill body is model
instructions, not code to execute. For builtin skills, whose paths sit outside
the project root, ``reyn.builtin.docs.read_builtin_body_bytes`` (#2913/#2914)
already short-circuits the out-of-project read gate for exactly the
``skills`` / ``pipelines`` body dirs. A ``run_skill`` verb would therefore be a
second execution surface duplicating that chain, with its own permission story
to get wrong. Discovery was the only missing link, so it is the only one added.

NOTE: there is NO ``skill__`` category and there never has been. This note used
to claim otherwise ("the RESOURCE category prefix for per-skill dynamic
dispatch, e.g. ``skill__code_review``"), and the claim propagated: #1647 cited
``skill__<name>`` as the precedent it was mirroring when it added one enumerated
action per MCP tool — a phantom, and one of the two reasons that PR should not
have landed (see ``universal_catalog``'s module docstring). Skills have never
cost a tool per skill; ``skill_management__list`` is the whole discovery
surface, and #3026 applied that same shape to corpora and pipelines.

Both verbs delegate to ``op_runtime/skill_install.py`` via the
``build_legacy_op_context`` bridge (same pattern as the mcp-install verbs).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import skill as _skill_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── skill_management__install_local ──────────────────────────────────────────

# Relocated to reyn.tools.descriptions.skill (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_SKILL_INSTALL_LOCAL_DESCRIPTION = _skill_descriptions.skill_install_local.text

_SKILL_INSTALL_LOCAL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": _skill_descriptions.PARAMS["skill_install_local"]["path"].text,
        },
        "name": {
            "type": "string",
            "description": _skill_descriptions.PARAMS["skill_install_local"]["name"].text,
        },
    },
    "required": ["path"],
}


async def _handle_skill_install_local(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Register a local skill directory by writing .reyn/config/skills.yaml.

    Delegates to op_runtime/skill_install.handle via build_legacy_op_context
    (same bridge pattern as mcp__install_local / mcp__install_registry). The
    handler resolves SKILL.md, threat-scans the description, gates the config
    write, writes the entry, records a config generation for crash-recovery,
    emits a skill_installed event, and requests a hot-reload.
    """
    from reyn.core.op_runtime.skill_install import handle as skill_install_handle
    from reyn.schemas.models import SkillInstallIROp
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
        op = SkillInstallIROp(
            kind="skill_install",
            path=path,
            name=name_override,
        )
    except Exception as exc:
        return {
            "status": "error",
            "data": {"error": f"invalid args: {exc}"},
        }

    decl = PermissionDecl()
    decl.file_write = [{"path": ".reyn/config/skills.yaml"}]

    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.actor = "skill_management__install_local"

    result = await skill_install_handle(op, op_ctx)
    return {"status": "ok", "data": result}


# ── skill_management__install_source ─────────────────────────────────────────

# Relocated to reyn.tools.descriptions.skill (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_SKILL_INSTALL_SOURCE_DESCRIPTION = _skill_descriptions.skill_install_source.text

_SKILL_INSTALL_SOURCE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": _skill_descriptions.PARAMS["skill_install_source"]["source"].text,
        },
        "name": {
            "type": "string",
            "description": _skill_descriptions.PARAMS["skill_install_source"]["name"].text,
        },
    },
    "required": ["source"],
}


async def _handle_skill_install_source(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Fetch and install a skill from a git/GitHub URL.

    Delegates to op_runtime/skill_install.handle via build_legacy_op_context
    (same bridge pattern as mcp__install_package). The handler:
      1. Gates require_http_get for the source host.
      2. Shallow-clones the repo to .reyn/skills/<name>/.
      3. Reads SKILL.md frontmatter from the clone (root or subdir via //).
      4. Threat-scans the description (scope=strict; block on blocking match).
      5. Gates require_file_write for .reyn/config/skills.yaml.
      6. Writes the skills.yaml entry with the installed clone path + source URL.
      7. Records a config generation for crash-recovery.
      8. Emits skill_installed event and requests a hot-reload.
    """
    from reyn.core.op_runtime.skill_install import handle as skill_install_handle
    from reyn.schemas.models import SkillInstallIROp
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

    try:
        op = SkillInstallIROp(
            kind="skill_install",
            source=source,
            name=name_override,
        )
    except Exception as exc:
        return {
            "status": "error",
            "data": {"error": f"invalid args: {exc}"},
        }

    decl = PermissionDecl()
    decl.file_write = [{"path": ".reyn/config/skills.yaml"}]
    # Declare http.get with wildcard — the source host is determined at call time.
    # The resolver gates the actual host via the wildcard path (= JIT prompt).
    decl.http_get = [{"host": "*"}]

    op_ctx = build_legacy_op_context(ctx)
    op_ctx.permission_decl = decl
    op_ctx.actor = "skill_management__install_source"

    result = await skill_install_handle(op, op_ctx)
    return {"status": "ok", "data": result}


# ── skill_list (#2971) ───────────────────────────────────────────────────────

_SKILL_LIST_DESCRIPTION = _skill_descriptions.skill_list.text

# No parameters: the result is already scoped to this session's registered set
# minus what the operator hid, so there is nothing for the caller to filter by.
# A `visibility` filter argument would be actively wrong — it would invite the
# model to ask for `hidden` skills, which this tool must never return.
_SKILL_LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


async def _handle_skill_list(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Return the model-visible skills: every registered entry whose
    ``visibility`` is not ``hidden``.

    Reads the SAME snapshot the L1 Skills menu renders from
    (``ctx.router_state.available_skills``, bound from
    ``RouterHostAdapter.get_available_skills()``), so three filters already
    apply before this handler sees an entry: ``enabled: false`` entries were
    dropped at registry build, and the per-session capability toggle (#2285)
    was applied by ``Session._reapply_skill_visibility``. This handler adds the
    one filter that is its own: ``hidden`` never leaves here.

    The complement is deliberate — ``menu`` skills are returned too, even
    though the model already has them in its system prompt. This tool answers
    "what skills exist for me", and a model that called it to check for a
    match should not have to cross-reference the menu to get a whole answer.
    """
    from reyn.data.skills.registry import VISIBILITY_DEFAULT, VISIBILITY_HIDDEN

    router_state = getattr(ctx, "router_state", None)
    entries = getattr(router_state, "available_skills", None) or []

    skills = [
        {
            "name": getattr(e, "name", ""),
            "description": getattr(e, "description", ""),
            "path": getattr(e, "path", ""),
        }
        for e in entries
        if getattr(e, "enabled", True)
        and getattr(e, "visibility", VISIBILITY_DEFAULT) != VISIBILITY_HIDDEN
    ]
    return {"skills": skills}


# ── ToolDefinitions ───────────────────────────────────────────────────────────

from reyn.core.offload.canonical import (  # noqa: E402
    skill_install_verb_to_canonical,
    skill_list_to_canonical,
)

# proposal 0060 D5d: mirrors the "skill" PartTypeSpec's doc_ref
# (reyn.core.part_types.skill) — same part-type, install-verb axis.
_SKILL_DOC_REF = "docs/concepts/tools-integrations/skills.md"

SKILL_INSTALL_LOCAL = ToolDefinition(
    canonical=skill_install_verb_to_canonical,
    name="skill_install_local",
    description=_SKILL_INSTALL_LOCAL_DESCRIPTION,
    parameters=_SKILL_INSTALL_LOCAL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_skill_install_local,
    category="io",
    purity="side_effect",
    doc_ref=_SKILL_DOC_REF,
)

SKILL_INSTALL_SOURCE = ToolDefinition(
    canonical=skill_install_verb_to_canonical,
    name="skill_install_source",
    description=_SKILL_INSTALL_SOURCE_DESCRIPTION,
    parameters=_SKILL_INSTALL_SOURCE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_skill_install_source,
    category="io",
    purity="side_effect",
    doc_ref=_SKILL_DOC_REF,
)

SKILL_LIST = ToolDefinition(
    canonical=skill_list_to_canonical,
    name="skill_list",
    description=_SKILL_LIST_DESCRIPTION,
    parameters=_SKILL_LIST_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_skill_list,
    category="discovery",
    purity="read_only",
    # A skill's description is operator/third-party-authored text that a
    # `skill_install_source` fetch can pull from a git repo — the same trust
    # boundary `list_mcp_tools` declares for server-authored descriptions.
    # It is threat-scanned at install, but this tool re-surfaces it later,
    # after a scan-rule update might have changed the verdict.
    returns_external_content=True,
    doc_ref=_SKILL_DOC_REF,
)

__all__ = ["SKILL_INSTALL_LOCAL", "SKILL_INSTALL_SOURCE", "SKILL_LIST"]
