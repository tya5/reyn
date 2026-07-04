"""skill_install kind handler — register a local skill directory into the project config.

Handler logic (one-shot, no sub-phases):
  1. Resolve SKILL.md: ``op.path`` may be a directory (→ ``<dir>/SKILL.md``)
     or a direct path to the SKILL.md file. Read it and extract frontmatter.
  2. Extract name (frontmatter ``name:`` key, else directory basename) and
     description (frontmatter ``description:`` key, else empty). Apply
     ``op.name`` override when set.
  3. Threat-scan the skill description via ``content_guard.scan_for_threats``
     (scope="strict") — block on a blocking-severity match.
  4. Gate via ``PermissionResolver.require_file_write`` for the skills.yaml path.
  5. Read ``.reyn/config/skills.yaml`` (or empty dict), set
     ``skills.entries.<name>`` = ``{path, description, enabled, auto_invoke}``,
     write back.
  6. ``record_config_generation`` on the skills.yaml path AFTER write —
     the truncation-surviving recovery base (#2259 / CLAUDE.md recovery gate).
  7. Emit ``skill_installed`` event (P6 audit trail).
  8. Request hot-reload so the installed skill goes live in the current session.

This is a P5 exception mirror of ``mcp_install``: ``.reyn/config/skills.yaml``
lives outside the workspace data channel but is written directly here (same
rationale — gated behind ``require_file_write`` + recorded via event for the
P6 audit trail).
"""
from __future__ import annotations

from pathlib import Path

from reyn.schemas.models import SkillInstallIROp

# Module-level import so tests can monkeypatch the threat-scan callables;
# the guard helpers are pure-function with no I/O and add negligible import cost.
from reyn.security.content_guard import first_blocking_match, scan_for_threats

from . import register
from .context import OpContext
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_skill_md(path_str: str) -> Path:
    """Resolve the SKILL.md path from an op.path value.

    If ``path_str`` points at a directory, returns ``<dir>/SKILL.md``.
    Otherwise returns the path as-is (direct file reference).
    """
    p = Path(path_str)
    if p.is_dir():
        return p / "SKILL.md"
    return p


def _read_skill_metadata(skill_md: Path) -> tuple[str, str]:
    """Read a SKILL.md file and return (name, description) from frontmatter.

    Returns empty strings for any field that is absent or unreadable —
    the caller applies the dir-basename fallback for name.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    from reyn.core.frontmatter import split_frontmatter
    fm, _body = split_frontmatter(text)
    name = str(fm.get("name") or "").strip()
    description = str(fm.get("description") or "").strip()
    return name, description


def _skills_config_path(project_root: Path) -> Path:
    """Canonical path for the dynamic skills registry config."""
    return project_root / ".reyn" / "config" / "skills.yaml"


def _resolve_project_root(workspace: object) -> Path:
    """Resolve the project root from a workspace object (mirrors mcp_install)."""
    root = getattr(workspace, "base_dir", None) or getattr(workspace, "root", None)
    return Path(root) if root is not None else Path.cwd()


def _read_yaml(path: Path) -> dict:
    """Read a YAML config file; return {} if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_yaml(path: Path, data: dict) -> None:
    """Write a dict as YAML to path, creating parent dirs as needed."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle(
    op: SkillInstallIROp,
    ctx: OpContext,
) -> dict:
    """Execute a skill_install op — register a local skill directory.

    Resolves the SKILL.md, scans for threats, gates the config write,
    persists the entry, records a config generation for crash-recovery,
    emits an audit event, and requests a hot-reload.
    """
    # ── 1. Resolve SKILL.md ───────────────────────────────────────────────────
    skill_md = _resolve_skill_md(op.path)
    if not skill_md.exists():
        return {
            "kind": "skill_install",
            "status": "error",
            "path": op.path,
            "error": (
                f"SKILL.md not found at '{skill_md}'. "
                "Provide the directory containing SKILL.md or the direct path."
            ),
        }

    # ── 2. Extract name + description from frontmatter ────────────────────────
    fm_name, description = _read_skill_metadata(skill_md)

    # Name resolution precedence: op.name override > frontmatter name > dir basename.
    if op.name:
        name = op.name.strip()
    elif fm_name:
        name = fm_name
    else:
        # Default: directory name (or stem of a direct SKILL.md reference)
        name = skill_md.parent.name if skill_md.name == "SKILL.md" else skill_md.stem

    if not name:
        return {
            "kind": "skill_install",
            "status": "error",
            "path": op.path,
            "error": "Could not determine skill name. Set a 'name:' in SKILL.md frontmatter or use op.name.",
        }

    # ── 3. Threat-scan the description (scope="strict") ──────────────────────
    _ts = getattr(ctx, "threat_scan", None)
    if _ts is not None and getattr(_ts, "enabled", False) and description:
        _matches = scan_for_threats(description, _ts, scope="strict")
        if _matches:
            for _m in _matches:
                ctx.events.emit(
                    "skill_install_threat_match",
                    pattern_id=_m.pattern_id,
                    severity=_m.severity,
                    scope=_m.scope,
                )
            _block = first_blocking_match(
                _matches, getattr(_ts, "block_severity", "block")
            )
            if _block is not None:
                ctx.events.emit(
                    "skill_install_threat_blocked",
                    pattern_id=_block.pattern_id,
                    severity=_block.severity,
                    name=name,
                )
                return {
                    "kind": "skill_install",
                    "status": "blocked",
                    "name": name,
                    "path": op.path,
                    "error": (
                        f"install blocked: SKILL.md description matched threat "
                        f"pattern '{_block.pattern_id}' "
                        f"({_block.scope}/{_block.severity}). The description "
                        f"contains a prohibited pattern. Do not install this skill."
                    ),
                }

    # ── 4. Permission gate ────────────────────────────────────────────────────
    project_root = _resolve_project_root(ctx.workspace)
    config_path = _skills_config_path(project_root)
    if ctx.permission_resolver is not None:
        _sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.actor,
            sandbox_policy=_sandbox,
        )

    # ── 5. Write skills.entries.<name> to .reyn/config/skills.yaml ───────────
    existing = _read_yaml(config_path)
    if "skills" not in existing or not isinstance(existing.get("skills"), dict):
        existing["skills"] = {}
    if "entries" not in existing["skills"] or not isinstance(existing["skills"].get("entries"), dict):
        existing["skills"]["entries"] = {}
    existing["skills"]["entries"][name] = {
        "path": str(Path(op.path).resolve()),
        "description": description,
        "enabled": True,
        "auto_invoke": True,
    }
    _write_yaml(config_path, existing)

    # ── 6. Record config generation for crash-recovery (#2259 / CLAUDE.md gate) ─
    from reyn.core.events.config_recovery import record_config_generation  # noqa: PLC0415
    await record_config_generation(getattr(ctx, "state_log", None), config_path, existing)

    # ── 7. Emit skill_installed event (P6) ────────────────────────────────────
    ctx.events.emit(
        "skill_installed",
        name=name,
        path=str(Path(op.path).resolve()),
        description=description,
        config_path=str(config_path),
    )

    # ── 8. Hot-reload: surface the installed skill in the current session ─────
    from reyn.runtime.hot_reload import get_active_hot_reloader  # noqa: PLC0415
    _reloader = get_active_hot_reloader()
    if _reloader is not None:
        _reloader.request_reload(source="skill_install")

    return {
        "status": "installed",
        "name": name,
        "path": str(Path(op.path).resolve()),
        "description": description,
        "config_path": str(config_path),
    }


register("skill_install", handle)
