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

    csub.add_parser(
        "validate",
        help="Check reyn.yaml/reyn.local.yaml/~/.reyn/config.yaml for unknown or renamed keys",
    )

    mg = csub.add_parser(
        "migrate",
        help="Rewrite renamed config keys to their current location (#4174 T0b)",
    )
    mg.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing any files.",
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
    elif sub == "validate":
        _validate()
    elif sub == "migrate":
        _migrate(dry_run=bool(getattr(args, "dry_run", False)))
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
        "model":           config.llm.model,
        "models":          config.llm.models,
        "api_base":        config.llm.api_base or "(not set)",
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
    dynamic_path = project_root / ".reyn" / "config" / "mcp.yaml"

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
    print("  → into .reyn/config/mcp.yaml (existing entries preserved)")
    print(
        f"  total servers in .reyn/config/mcp.yaml after migration: "
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


def _validate() -> None:
    """#4174 T0: report every unknown/renamed config key found in the
    operator-editable POLICY TIER (reyn.yaml / reyn.local.yaml /
    ~/.reyn/config.yaml). #4231 (C): also report every KNOWN policy-tier
    key whose value is silently inert under another key's current value
    (a DIFFERENT defect class — the key is real and correctly spelled,
    but the configuration as a whole makes it a no-op). #4235: also
    report unknown/renamed keys, separately again, for the hot-reload
    IN-set (``.reyn/{mcp,cron,hooks,skills,pipelines,presentations}.yaml``)
    — a DIFFERENT config surface with a DIFFERENT remedy (edit-and-restart
    for the policy tier vs. edit-and-next-turn for the IN-set), so all
    three findings are reported as separate labeled sections rather than
    merged into one dict: merging would lose exactly the "which one do I
    fix, and how" information a finding needs to be actionable
    (lead-coder/docs-maintainer ruling, #4235).

    Policy-tier and IN-set unknown-key detection both use the SAME
    ``unknown_config_keys`` (config_schema.py) — a tier is just a raw dict
    to it, and #4235's own investigation confirmed the IN-set's merged
    dict (``load_hot_reload_config``) uses the identical top-level key
    vocabulary (``mcp``/``cron``/``hooks``/... are ordinary ``ReynConfig``
    fields too) — no new validation logic, only a second call with a
    second load. ``disabled_config_keys`` (#4231 C) is checked against the
    policy tier only — the one dependency it currently knows about
    (``action_retrieval.universal_wrappers_enabled`` vs.
    ``tool_use.scheme``) lives there.

    Uses ``build_policy_tier_config`` — the SAME construction
    ``load_config``'s own startup warning uses (architect's explicit
    requirement: this command must check exactly what startup checks, not
    a second hand-reconstructed merge) — and ``load_hot_reload_config``,
    the same loader the hot-reload mechanism itself uses to build the
    IN-set every turn. Exits 0 regardless of findings (owner ruling: warn,
    never hard-fail — this command REPORTS, it does not gate anything).

    ⚠️ Known remaining gap (#4235's own recorded scope, not closed here):
    the #4194 CUI status indicator counts POLICY-TIER findings only (tui
    decision) — this PR widens ``validate``'s own scope to the IN-set
    without widening the indicator to match, so the two surfaces'
    coverage diverges again (the indicator undercounts relative to what
    ``validate`` can now show). Left as-is; closing it is a separate
    decision.
    """
    from reyn.config.config_schema import disabled_config_keys, unknown_config_keys
    from reyn.config.loader import build_policy_tier_config, load_hot_reload_config

    policy_merged = build_policy_tier_config()
    policy_unknown = unknown_config_keys(policy_merged)
    disabled = disabled_config_keys(policy_merged)
    in_set_merged = load_hot_reload_config()
    in_set_unknown = unknown_config_keys(in_set_merged)

    if not policy_unknown and not disabled and not in_set_unknown:
        print("No unknown, renamed, or disabled-by-dependency config keys found.")
        return

    if policy_unknown:
        print(
            f"Policy tier (reyn.yaml / reyn.local.yaml / ~/.reyn/config.yaml) — "
            f"{len(policy_unknown)} unrecognized config key(s):\n"
        )
        for key, hint in sorted(policy_unknown.items()):
            print(f"  {key}")
            if hint:
                print(f"    -> {hint.note}")
            else:
                print("    -> not applied; see 'reyn config fields' for valid keys.")
        print("\nRun 'reyn config migrate' to fix renamed keys automatically.")

    if disabled:
        if policy_unknown:
            print()
        print(
            f"Found {len(disabled)} config key(s) that are known but currently "
            f"have no effect:\n"
        )
        for key, disabled_hint in sorted(disabled.items()):
            print(f"  {key}")
            print(f"    -> {disabled_hint.note}")
            print(f"    -> depends on: {disabled_hint.dependency_key}")
            print(f"    -> fix: {disabled_hint.fix}")

    if in_set_unknown:
        if policy_unknown or disabled:
            print()
        print(
            f"Hot-reload IN-set (.reyn/{{mcp,cron,hooks,skills,pipelines,"
            f"presentations}}.yaml) — {len(in_set_unknown)} unrecognized "
            f"config key(s):\n"
        )
        for key, hint in sorted(in_set_unknown.items()):
            print(f"  {key}")
            if hint:
                print(f"    -> {hint.note}")
            else:
                print("    -> not applied; see 'reyn config fields' for valid keys.")
        print(
            "\nIN-set keys apply on the next turn automatically (no restart, "
            "no 'reyn config migrate' support for this tier — edit the "
            ".reyn/*.yaml file directly)."
        )


def _migrate(*, dry_run: bool = False) -> None:
    """#4174 T0b: rewrite renamed top-level config keys (per
    ``config_schema._RENAMED_CONFIG_KEYS``) to their current location, in
    place, in whichever file each key was actually found (reyn.yaml /
    reyn.local.yaml / ~/.reyn/config.yaml — the same 3 files
    ``migrate-mcp`` scans).

    Generalizes ``_migrate_mcp``'s pattern (architect's explicit precedent)
    to the renamed-key registry rather than one hand-written mcp-only
    migration. Correctly reports "nothing to migrate" both when no renames
    are registered yet AND when a project's config has no renamed key in
    use — the two are different reasons for the same outcome, both real
    (lead-coder's explicit requirement: this must not be silently vacuous).

    Scope note: an entry is only auto-rewritten when its
    :class:`~reyn.config.config_schema.RenamedKeyHint.destination` is set
    — a plain rename with no value transform. ``destination=None`` (e.g.
    ``_RENAMED_SANDBOX_POLICY_KEYS``'s boolean-inversion renames, wrapped
    via ``config.infra._sandbox_policy_freeform_validator``) means the
    rename carries a per-key value transform this command must not guess
    at; those are reported as "needs manual review" instead
    (lead-coder's block on #4190 — encoding the destination-vs-note
    distinction as a TYPE field, not a syntactic proxy like "does the hint
    string contain a space", so a future T1-T6 entry can't accidentally
    become auto-rewritten by writing a space-free note that was never
    meant as a destination). ``_RENAMED_CONFIG_KEYS`` is empty today
    (T1-T6 populate it incrementally); ``_RENAMED_SANDBOX_POLICY_KEYS`` is
    intentionally NOT auto-rewritten by this command — an operator on an
    old sandbox.policy key sees the guidance via ``reyn config validate``
    and fixes it by hand.
    """
    import yaml

    from reyn.config import _find_project_root
    from reyn.config.config_schema import _RENAMED_CONFIG_KEYS

    if not _RENAMED_CONFIG_KEYS:
        print(
            "No config key renames are registered yet (nothing to migrate "
            "— #4174 T1-T6 populate this registry incrementally)."
        )
        return

    # Only entries with a non-None `destination` (a plain rename, no value
    # transform) are safe to auto-rewrite — see the docstring above.
    auto_rewritable = {
        old: hint.destination for old, hint in _RENAMED_CONFIG_KEYS.items()
        if hint.destination is not None
    }
    needs_manual = sorted(set(_RENAMED_CONFIG_KEYS) - set(auto_rewritable))

    project_root = _find_project_root(Path.cwd())
    candidates = [Path.home() / ".reyn" / "config.yaml"]
    if project_root is not None:
        candidates = [project_root / "reyn.yaml", project_root / "reyn.local.yaml", *candidates]

    def _read(p: Path) -> dict:
        if not p.exists():
            return {}
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _pop_dotted(d: dict, dotted: str):
        """Remove *dotted* from *d* if present; return (found, value)."""
        parts = dotted.split(".")
        node = d
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return False, None
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            return False, None
        return True, node.pop(parts[-1])

    def _set_dotted(d: dict, dotted: str, value) -> None:
        parts = dotted.split(".")
        node = d
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def _label(path: Path) -> str:
        try:
            return str(path.relative_to(project_root)) if project_root else str(path)
        except ValueError:
            return str(path)

    any_changes = False
    manual_found: list[tuple[str, str]] = []
    for path in candidates:
        cfg = _read(path)
        if not cfg:
            continue
        changed_here = []
        for old_key, new_key in auto_rewritable.items():
            found, value = _pop_dotted(cfg, old_key)
            if not found:
                continue
            _set_dotted(cfg, new_key, value)
            changed_here.append((old_key, new_key))
        for old_key in needs_manual:
            found, _value = _pop_dotted(dict(cfg), old_key)  # peek, don't mutate
            if found:
                manual_found.append((_label(path), old_key))
        if not changed_here:
            continue
        any_changes = True
        print(f"{_label(path)}:")
        for old_key, new_key in changed_here:
            print(f"  {old_key} -> {new_key}")
        if not dry_run:
            path.write_text(
                yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )

    if manual_found:
        print("\nThe following renamed key(s) need manual review (value "
              "transform, not a plain rename — see 'reyn config validate' "
              "for guidance):")
        for label, old_key in manual_found:
            print(f"  {label}: {old_key} — {_RENAMED_CONFIG_KEYS[old_key].note}")

    if not any_changes and not manual_found:
        print("No renamed keys found in your config — nothing to migrate.")
        return
    if any_changes:
        if dry_run:
            print("\nDry run only — no files written. Re-run without --dry-run to apply.")
        else:
            print("\nDone.")


def _set(key: str, value: str) -> None:
    """Set a config key in reyn.local.yaml.

    Validates *key* against the full ReynConfig schema (including nested
    keys like ``safety.loop.max_router_calls_per_turn`` and free-form dict
    sub-keys like ``mcp.servers.github.url``).

    Writes the correct nested YAML structure — ``safety.loop.max_router_calls_per_turn``
    becomes ``{safety: {loop: {max_router_calls_per_turn: <value>}}}`` rather than
    the flat ``{safety: {'loop.max_router_calls_per_turn': <value>}}`` the old
    1-level split produced.
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
    # ``safety.loop.max_router_calls_per_turn`` writes {safety: {loop: {max_router_calls_per_turn: v}}}
    # instead of {safety: {'loop.max_router_calls_per_turn': v}}.
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
