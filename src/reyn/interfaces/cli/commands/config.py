"""`reyn config` — view and edit reyn configuration."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reyn.config import load_config
from reyn.config.config_schema import is_valid_config_key, resolve_config_value, walk_config_schema


def register(sub) -> None:
    p = sub.add_parser("config", help="View and edit reyn configuration")
    csub = p.add_subparsers(dest="config_cmd", metavar="<subcommand>")
    p.set_defaults(func=run)

    csub.add_parser("show", help="Show current effective config (merged from all sources)")
    csub.add_parser("fields", help="List all config fields with descriptions and examples")

    g = csub.add_parser("get", help="Get a single config value")
    g.add_argument("key", metavar="KEY", help="Config key (e.g. model, api_base)")

    s = csub.add_parser("set", help="Set a config value in reyn.local.yaml")
    s.add_argument("key", metavar="KEY",
                   help="Config key (e.g. api_base, models.standard). Run 'reyn config fields' for the full list.")
    s.add_argument("value", metavar="VALUE", help="Value to set (YAML syntax accepted)")

    m = csub.add_parser(
        "migrate-mcp",
        help=(
            "Move legacy mcp.servers entries from reyn.yaml / reyn.local.yaml "
            "to .reyn/mcp.yaml (issue #470 config separation)"
        ),
    )
    m.add_argument(
        "--dry-run", action="store_true",
        help="Show what would move without writing any files.",
    )


def run(args: argparse.Namespace) -> None:
    sub = getattr(args, "config_cmd", None)
    if sub == "fields":
        _fields()
    elif sub == "show":
        _show()
    elif sub == "get":
        _get(args.key)
    elif sub == "set":
        _set(args.key, args.value)
    elif sub == "migrate-mcp":
        _migrate_mcp(dry_run=bool(getattr(args, "dry_run", False)))
    else:
        _show()


def _fields() -> None:
    """List all config fields derived from the live ReynConfig schema."""
    import dataclasses as _dc
    W_KEY, W_TYPE, W_DEF = 46, 14, 20
    header = f"{'Field':<{W_KEY}}  {'Type':<{W_TYPE}}  {'Default':<{W_DEF}}  Description"
    print(header)
    print("─" * len(header))
    for node in walk_config_schema():
        default_str = repr(node.default) if node.default is not _dc.MISSING else "(required)"
        if len(default_str) > W_DEF:
            default_str = default_str[:W_DEF - 1] + "…"
        kind = "(free-form dict)" if node.is_dict_leaf else node.type_repr
        print(f"{node.key:<{W_KEY}}  {kind:<{W_TYPE}}  {default_str:<{W_DEF}}  {node.desc}")


def _show() -> None:
    import yaml
    config = load_config()
    effective = {
        "model":           config.model,
        "models":          config.models,
        "api_base":        config.api_base or "(not set)",
        "output_language": config.output_language or "(not set — chat router skips language directive; phase paths default to ja)",
        "permissions":     config.permissions,
        "mcp":             config.mcp if config.mcp else "(not configured)",
    }
    print("# Effective config (merged from all sources)")
    print(yaml.dump(effective, allow_unicode=True, default_flow_style=False), end="")


def _get(key: str) -> None:
    """Get a config value by dotted key.

    Distinguishes "key exists with value None" from "key does not exist
    in the schema" — the old ``getattr(config, key, None)`` conflated them.
    """
    import yaml
    config = load_config()
    found, value = resolve_config_value(config, key)
    if not found:
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        print("Run 'reyn config fields' to see available keys.", file=sys.stderr)
        sys.exit(1)
    if value is None:
        print("(not set)")
    elif isinstance(value, (dict, list)):
        print(yaml.dump(value, allow_unicode=True, default_flow_style=False), end="")
    else:
        print(value)


def _migrate_mcp(*, dry_run: bool = False) -> None:
    """Move legacy ``mcp.servers`` entries from ``reyn.yaml`` /
    ``reyn.local.yaml`` (and ``~/.reyn/config.yaml``) into the canonical
    ``.reyn/mcp.yaml`` location (= issue #470 config separation).

    Why: post-#470, the dynamic MCP server registry lives at
    ``.reyn/mcp.yaml`` so ``reyn.yaml`` carries only static deployment
    config. Existing projects continue to load legacy entries (=
    backward compat), but operators who want the clean separation
    today can run this command to migrate explicitly.

    Behaviour:
      - Reads ``mcp.servers`` from reyn.yaml + reyn.local.yaml +
        ~/.reyn/config.yaml (= the legacy locations).
      - Merges into ``.reyn/mcp.yaml`` (= entries already present
        there win on conflict so a partial migration doesn't get
        clobbered).
      - Removes the ``mcp.servers`` section from each legacy file
        (= leaves other config sections intact).
      - On ``--dry-run``: prints the plan without writing.

    Non-goals:
      - Auto-migration on every load (= explicit by design; operator
        decides when to clean up the diff).
      - Removing the ``mcp:`` key entirely from legacy files (=
        leaves ``mcp:`` with sibling keys like ``mcp_servers_extra``
        intact if any exist; only the ``servers`` sub-key is moved).
    """
    import yaml

    from reyn.config import _find_project_root

    project_root = _find_project_root(Path.cwd())
    if project_root is None:
        print(
            "Error: no Reyn project root found. Run from a directory with "
            "reyn.yaml or .reyn/.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Legacy paths to scan: project + local + user-global.
    legacy_paths = [
        project_root / "reyn.yaml",
        project_root / "reyn.local.yaml",
        Path.home() / ".reyn" / "config.yaml",
    ]
    dynamic_path = project_root / ".reyn" / "mcp.yaml"

    def _read(p: Path) -> dict:
        if not p.exists():
            return {}
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # Collect legacy servers per file (for the move-out step).
    legacy_by_file: dict[Path, dict] = {}
    for p in legacy_paths:
        cfg = _read(p)
        servers = (
            cfg.get("mcp", {}).get("servers", {})
            if isinstance(cfg.get("mcp"), dict)
            else {}
        )
        if isinstance(servers, dict) and servers:
            legacy_by_file[p] = dict(servers)

    if not legacy_by_file:
        print("No legacy mcp.servers entries found — nothing to migrate.")
        return

    # Compose target: existing dynamic file's entries (= take precedence
    # so a partial prior migration isn't clobbered) plus the legacy
    # entries that aren't already there.
    dynamic_cfg = _read(dynamic_path)
    dynamic_servers = (
        dynamic_cfg.get("mcp", {}).get("servers", {})
        if isinstance(dynamic_cfg.get("mcp"), dict)
        else {}
    )
    if not isinstance(dynamic_servers, dict):
        dynamic_servers = {}

    merged_dynamic = dict(dynamic_servers)
    for _src, src_servers in legacy_by_file.items():
        for name, entry in src_servers.items():
            if name not in merged_dynamic:
                merged_dynamic[name] = entry

    # Print plan.
    print(f"# Migration plan ({'DRY RUN' if dry_run else 'WRITING'})")
    for src, src_servers in legacy_by_file.items():
        try:
            rel = src.relative_to(project_root)
            src_label = str(rel)
        except ValueError:
            src_label = str(src)
        moved = sorted(src_servers.keys())
        print(f"  from {src_label}: {len(moved)} server(s) → {', '.join(moved)}")
    print("  → into .reyn/mcp.yaml (existing entries preserved)")
    print(
        f"  total servers in .reyn/mcp.yaml after migration: "
        f"{len(merged_dynamic)}"
    )

    if dry_run:
        print("\nDry run only — no files written. Re-run without --dry-run to apply.")
        return

    # Write the merged dynamic file.
    dynamic_path.parent.mkdir(parents=True, exist_ok=True)
    new_dynamic = dict(dynamic_cfg)
    new_dynamic_mcp = dict(new_dynamic.get("mcp", {})) if isinstance(new_dynamic.get("mcp"), dict) else {}
    new_dynamic_mcp["servers"] = merged_dynamic
    new_dynamic["mcp"] = new_dynamic_mcp
    dynamic_path.write_text(
        yaml.dump(new_dynamic, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    print(f"\nWrote {dynamic_path}")

    # Remove ``mcp.servers`` from each legacy file (= leave other keys intact).
    for src in legacy_by_file:
        cfg = _read(src)
        mcp_section = cfg.get("mcp")
        if isinstance(mcp_section, dict) and "servers" in mcp_section:
            del mcp_section["servers"]
            # If the mcp section is now empty, drop it entirely so the
            # legacy file doesn't keep a dangling ``mcp: {}`` key.
            if not mcp_section:
                del cfg["mcp"]
            else:
                cfg["mcp"] = mcp_section
        src.write_text(
            yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        try:
            rel = src.relative_to(project_root)
            src_label = str(rel)
        except ValueError:
            src_label = str(src)
        print(f"Removed mcp.servers from {src_label}")


def _set(key: str, value: str) -> None:
    """Set a config key in reyn.local.yaml.

    Validates *key* against the full ReynConfig schema (including nested
    keys like ``safety.loop.max_phase_visits`` and free-form dict sub-keys
    like ``mcp.servers.github.url``).

    Writes the correct nested YAML structure — ``safety.loop.max_phase_visits``
    becomes ``{safety: {loop: {max_phase_visits: <value>}}}`` rather than
    the flat ``{safety: {'loop.max_phase_visits': <value>}}`` the old 1-level
    split produced.
    """
    import yaml

    if not is_valid_config_key(key):
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        print("Run 'reyn config fields' to see available keys.", file=sys.stderr)
        sys.exit(1)

    from reyn.config import _find_project_root
    project_root = _find_project_root(Path.cwd()) or Path.cwd()
    local_cfg = project_root / "reyn.local.yaml"
    current: dict = {}
    if local_cfg.exists():
        current = yaml.safe_load(local_cfg.read_text(encoding="utf-8")) or {}

    try:
        parsed = yaml.safe_load(value)
    except Exception:
        parsed = value

    # Recurse the dotted path through nested dicts via setdefault so that
    # ``safety.loop.max_phase_visits`` writes {safety: {loop: {max_phase_visits: v}}}
    # instead of {safety: {'loop.max_phase_visits': v}}.
    parts = key.split(".")
    node: dict = current
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    node[parts[-1]] = parsed

    local_cfg.write_text(yaml.dump(current, allow_unicode=True, default_flow_style=False),
                         encoding="utf-8")
    print(f"Set {key} = {parsed!r}  →  {local_cfg}")
