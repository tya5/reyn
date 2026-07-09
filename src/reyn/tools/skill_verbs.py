"""Skill verb-object handlers — local install (#2548 PR-C) + source/git install (#2548 PR-D).

Router-callable skill management verbs under the ``skill_management`` category.
Exposes two install verbs:

  - ``skill_management__install_local`` — register a local skill directory
    (one containing a ``SKILL.md`` file) into the project
    ``.reyn/config/skills.yaml``, making it available to sessions
    that load the config cascade.

  - ``skill_management__install_source`` — fetch a skill from a git/GitHub URL,
    install it into ``.reyn/skills/<name>/``, and register the installed copy.
    Requires a ``require_http_get`` gate for the source host + the
    ``require_file_write`` gate for skills.yaml.

NOTE: ``skill__`` is the RESOURCE category prefix used for per-skill dynamic
dispatch (e.g. ``skill__code_review``). Management operations use
``skill_management__`` to avoid colliding with that resource namespace —
mirrors ``mcp__`` (management) vs dynamic ``mcp.<server>.<tool>`` (resource).

Both verbs delegate to ``op_runtime/skill_install.py`` via the
``build_legacy_op_context`` bridge (same pattern as the mcp-install verbs).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── skill_management__install_local ──────────────────────────────────────────

_SKILL_INSTALL_LOCAL_DESCRIPTION = (
    "Register a local skill directory into the project config "
    "by reading its SKILL.md frontmatter and writing an entry to "
    ".reyn/config/skills.yaml. The skill is immediately available "
    "to sessions after the next hot-reload. Pass the path to the "
    "directory containing SKILL.md (or the SKILL.md file directly). "
    "Use 'name' to override the config key when the directory name "
    "differs from the desired skill identifier."
)

_SKILL_INSTALL_LOCAL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Path to the skill directory (containing SKILL.md) or "
                "the direct path to the SKILL.md file. May be absolute "
                "or project-root-relative."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Config key written under skills.entries.<name>. "
                "When omitted, the frontmatter 'name:' field is used; "
                "if that is also absent, the directory basename is used."
            ),
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

_SKILL_INSTALL_SOURCE_DESCRIPTION = (
    "Fetch a skill from a git/GitHub URL and install it into the project. "
    "The repo is shallow-cloned to .reyn/skills/<name>/, the SKILL.md is "
    "threat-scanned, and an entry is written to .reyn/config/skills.yaml. "
    "The skill is immediately available to sessions after the next hot-reload. "
    "Requires http.get permission for the source host in the skill's frontmatter. "
    "Source format: 'https://github.com/user/repo' (repo root must contain SKILL.md) "
    "or 'https://github.com/user/repo//path/to/skill' (subdir with SKILL.md). "
    "Use 'name' to override the config key when the default (from SKILL.md frontmatter "
    "or repo/subdir basename) differs from the desired skill identifier."
)

_SKILL_INSTALL_SOURCE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": (
                "Git or GitHub URL of the skill repo. The root (or subdir "
                "specified via '//' separator) must contain a SKILL.md file. "
                "Examples: 'https://github.com/user/skill-repo' or "
                "'https://github.com/user/monorepo//skills/my-skill'."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Config key written under skills.entries.<name>. "
                "When omitted, the frontmatter 'name:' field is used; "
                "if that is also absent, the repo/subdir basename is used."
            ),
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


# ── ToolDefinitions ───────────────────────────────────────────────────────────

from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

SKILL_INSTALL_LOCAL = ToolDefinition(
    canonical=STRUCTURED_PASSTHROUGH,
    name="skill_install_local",
    description=_SKILL_INSTALL_LOCAL_DESCRIPTION,
    parameters=_SKILL_INSTALL_LOCAL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_skill_install_local,
    category="io",
    purity="side_effect",
)

SKILL_INSTALL_SOURCE = ToolDefinition(
    canonical=STRUCTURED_PASSTHROUGH,
    name="skill_install_source",
    description=_SKILL_INSTALL_SOURCE_DESCRIPTION,
    parameters=_SKILL_INSTALL_SOURCE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_skill_install_source,
    category="io",
    purity="side_effect",
)

__all__ = ["SKILL_INSTALL_LOCAL", "SKILL_INSTALL_SOURCE"]
