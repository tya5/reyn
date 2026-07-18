"""plugin_uninstall kind handler — inverse of plugin_install (ADR 0064 §3.7/§3.9/§3.11).

Drop-registry-first, then remove-copy (§3.11): every ``.reyn/config/
{mcp,pipelines,skills}.yaml`` entry tagged ``plugin_id == name`` (§3.7's
additive provenance field, stamped by ``plugin_install`` on every entry it
registers) is removed BEFORE the ``~/.reyn/plugins/<name>/`` copy is
deleted — so an interrupted uninstall never leaves a live registry entry
pointing at a deleted copy (the crash-safety direction the ADR calls out
explicitly).

Not WAL-derived (§3.11): same rationale as ``plugin_install`` — these are
file/registry mutations, not WAL-event-derived state, so the CLAUDE.md
truncate-falsify recovery gate does not apply.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from reyn.schemas.models import PluginUninstallIROp

from . import register
from .context import OpContext
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx
from .plugin_install import plugins_root
from .skill_install import _read_yaml, _resolve_project_root, _write_yaml


def _registry_paths(project_root: Path) -> "dict[str, Path]":
    config_dir = project_root / ".reyn" / "config"
    return {
        "mcp": config_dir / "mcp.yaml",
        "pipelines": config_dir / "pipelines.yaml",
        "skills": config_dir / "skills.yaml",
    }


def _entries_section(data: dict, top_key: str) -> "dict | None":
    """Return the ``<top_key>.entries``/``<top_key>.servers`` mapping (mcp
    uses ``servers``, pipelines/skills use ``entries``), or None when absent
    / malformed."""
    section = data.get(top_key)
    if not isinstance(section, dict):
        return None
    entries_key = "servers" if top_key == "mcp" else "entries"
    entries = section.get(entries_key)
    return entries if isinstance(entries, dict) else None


async def _drop_plugin_entries(
    registry_kind: str, config_path: Path, plugin_name: str, ctx: OpContext,
) -> list[str]:
    """Remove every entry tagged ``plugin_id == plugin_name`` from one
    registry file. Returns the removed entry names."""
    if not config_path.exists():
        return []
    data = _read_yaml(config_path)
    entries = _entries_section(data, registry_kind)
    if not entries:
        return []
    to_remove = [
        name for name, entry in entries.items()
        if isinstance(entry, dict) and entry.get("plugin_id") == plugin_name
    ]
    if not to_remove:
        return []

    if ctx.permission_resolver is not None:
        sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(config_path), ctx.actor,
            sandbox_policy=sandbox, bus=ctx.intervention_bus,
        )

    for name in to_remove:
        del entries[name]
    _write_yaml(config_path, data)

    from reyn.core.events.config_recovery import record_config_generation
    await record_config_generation(getattr(ctx, "state_log", None), config_path, data)

    return to_remove


async def handle(op: PluginUninstallIROp, ctx: OpContext) -> dict:
    project_root = _resolve_project_root(ctx.workspace)
    name = op.name.strip()
    if not name:
        return {"kind": "plugin_uninstall", "status": "error", "error": "name is required"}

    ctx.events.emit("plugin_uninstall_started", name=name)

    # ── 1. Drop registry entries FIRST (§3.11 crash-safety ordering) ─────────
    removed: dict[str, list[str]] = {}
    for registry_kind, config_path in _registry_paths(project_root).items():
        removed[registry_kind] = await _drop_plugin_entries(registry_kind, config_path, name, ctx)

    if any(removed.values()):
        from reyn.runtime.hot_reload import dispatch_install_reload
        # A drop is a same-name-removal, not a pure addition — it always
        # takes the deferred turn-boundary reload path (mirrors
        # mcp_drop_server's non-immediate-apply behavior); dispatch_install_reload
        # with is_addition=False routes there uniformly across all three seams.
        for registry_kind in ("mcp", "pipelines", "skills"):
            if removed.get(registry_kind):
                seam_source = {
                    "mcp": "mcp__install_local", "pipelines": "pipeline_install",
                    "skills": "skill_install",
                }[registry_kind]
                await dispatch_install_reload(
                    getattr(ctx, "hot_reloader", None), source=seam_source, is_addition=False,
                )

    ctx.events.emit("plugin_uninstall_registry_dropped", name=name, removed=removed)

    # ── 2. Remove the global copy ─────────────────────────────────────────────
    plugin_root = plugins_root() / name
    copy_removed = plugin_root.is_dir()
    if copy_removed:
        if ctx.permission_resolver is not None:
            sandbox = _sandbox_policy_from_ctx(ctx)
            await ctx.permission_resolver.require_file_write(
                ctx.permission_decl, str(plugin_root), ctx.actor,
                sandbox_policy=sandbox, bus=ctx.intervention_bus,
            )
        shutil.rmtree(plugin_root, ignore_errors=True)

    ctx.events.emit("plugin_uninstall_completed", name=name, copy_removed=copy_removed)

    return {
        "status": "uninstalled",
        "name": name,
        "removed": removed,
        "copy_removed": copy_removed,
    }


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("plugin_uninstall", handle, canonical=STRUCTURED_PASSTHROUGH)
