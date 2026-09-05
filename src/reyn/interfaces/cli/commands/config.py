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


# #4631: a shape-based detector — `mcp.<name>` written where
# `mcp.servers.<name>` belongs is invisible to `unknown_config_keys`
# (which walks the TOP-LEVEL `ReynConfig` schema only; `mcp` itself IS a
# recognized key, so nothing under it is ever inspected, same class of
# gap #4501 closed for hooks[] entries). architect's own discriminator:
# a dict directly under `mcp:` (other than the real `servers` key)
# carrying `command`/`url`/`type` is shaped like a server entry, not a
# scalar config knob (`mcp.timeout_seconds` etc. never take that shape).
_MCP_SERVER_ENTRY_SHAPE_KEYS: "tuple[str, ...]" = ("command", "url", "type")


def _mcp_misplaced_server_entries(mcp_section: object) -> "list[str]":
    """Every key directly under a raw ``mcp:`` dict (other than the real
    ``servers`` key) whose value is SHAPED like a server entry — the
    #4631 defect: written at ``mcp.<name>`` instead of
    ``mcp.servers.<name>``, loaded without error and without warning
    (``cfg.mcp.servers`` silently stays empty)."""
    if not isinstance(mcp_section, dict):
        return []
    found = []
    for key, value in mcp_section.items():
        if key == "servers" or not isinstance(value, dict):
            continue
        if any(shape_key in value for shape_key in _MCP_SERVER_ENTRY_SHAPE_KEYS):
            found.append(key)
    return found


# #4604: reyn's own MCP transport-type vocabulary renamed "http" ->
# "streamable-http" (aligning with the Agent Plugins 1.0 canonical
# mcp.schema.json). mcp/client.py's MCPClient.__init__ already REJECTS
# the old value at connection time (a clear ValueError naming the rename)
# -- this is the earlier, proactive half: `reyn config validate` can find
# a stale `type: http` entry without ever trying to connect, closing the
# #4401 shape (a server discovered degraded only much later) for THIS
# specific value, not just a generic "unsupported type" surprise.
def _mcp_renamed_http_transport_entries(mcp_section: object) -> "list[str]":
    """Every ``mcp.servers.<name>`` entry (in a raw, per-source ``mcp:``
    dict) whose ``type`` is still the pre-#4604 value ``"http"``."""
    if not isinstance(mcp_section, dict):
        return []
    servers = mcp_section.get("servers")
    if not isinstance(servers, dict):
        return []
    return [
        name for name, entry in servers.items()
        if isinstance(entry, dict) and entry.get("type") == "http"
    ]


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
    (``tool_use.universal_wrappers_enabled`` vs. ``tool_use.scheme`` —
    #4552 PR-3: both now live under the same top-level ``tool_use:`` key,
    relocated from ``action_retrieval.universal_wrappers_enabled``)
    lives there.

    #4501: ``unknown_config_keys`` walks the TOP-LEVEL ``ReynConfig``
    schema only — it confirms ``hooks:`` itself is a recognized key, but
    never recurses into what each ``hooks:`` LIST ENTRY contains (a
    free-form list of dicts, not individual dataclass fields the schema
    walker sees). That gap is exactly what let an operator's
    ``allow_write_paths`` (the agent-level sandbox.policy field name,
    written at the wrong per-hook site) pass ``validate`` silently while
    doing nothing — architect's own real incident. #4501 closed it for
    ONE of hooks' real input paths: the ``.reyn/config/hooks.yaml``
    runtime IN-set. #4364 PR-1 found two more the same night —
    ``_build_hook_registry``'s own (then-3-layer, now #5505's 5-layer)
    COMBINE names them: reyn.yaml's own top-level ``hooks:`` (the
    layer ``docs/concepts/runtime/hooks.md`` actually tells operators to
    write in, and architect's own real incident lived there, not in the
    IN-set #4501 fixed) and every ``.reyn/agents/<name>/hooks.yaml``. #5505
    added a 4th: every ``.reyn/config/agents/<name>/hooks.yaml`` (the
    trusted per-agent layer) — the same reasoning applies unchanged (a
    malformed entry there would otherwise only surface at the NEXT boot,
    refusing it per that layer's own fail-loud contract, with no earlier
    warning). All four now feed through ``load_hooks`` (the SAME parser
    hook-loading itself uses, per this function's own "check exactly what
    startup checks" discipline) and any ``HookConfigError`` per source is
    reported as a labeled section — never raised: this command REPORTS,
    per the docstring above, so a malformed hook entry is caught here
    rather than only at the next actual hook-load.

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
    from reyn.config.loader import (
        _CheckedElsewhere,
        _find_project_root,
        _load_yaml,
        build_policy_tier_config,
        load_hot_reload_config,
        load_per_agent_hooks,
        load_trusted_per_agent_hooks,
    )
    from reyn.hooks.loader import load_hooks
    from reyn.hooks.schema import HookConfigError

    # lead-coder review (#4555): root resolution must be consistent across
    # all 3 sources this command reads, or `cd subdir && reyn config
    # validate` checks reyn.yaml (build_policy_tier_config walks up to the
    # real root via _find_project_root) while silently skipping the other 2
    # (a bare Path.cwd() does not walk up). Resolve once, thread everywhere.
    project_root = _find_project_root(Path.cwd()) or Path.cwd()

    policy_merged = build_policy_tier_config()
    policy_unknown = unknown_config_keys(policy_merged)
    disabled = disabled_config_keys(policy_merged)
    in_set_merged = load_hot_reload_config(project_root)
    in_set_unknown = unknown_config_keys(in_set_merged)

    # #4501/#4364 PR-1: hooks[] entries are a free-form list the top-level
    # schema walk above never opens — feed EVERY real input path through the
    # real parser to catch a malformed/wrong-scope key inside one, same as an
    # actual hook-load would. Four sources, mirroring
    # Session._build_hook_registry's own startup/runtime/trusted-per-agent/
    # per-agent layers (this command has no per-SESSION source — there is
    # no live session to read one from):
    hook_entry_errors: dict[str, str] = {}

    # (1) reyn.yaml top-level `hooks:` (the startup layer — the one
    # docs/concepts/runtime/hooks.md tells operators to write in; #4501 did
    # NOT cover this source, only (2) below).
    policy_hooks_raw = policy_merged.get("hooks")
    if policy_hooks_raw:
        try:
            load_hooks(policy_hooks_raw)
        except HookConfigError as exc:
            hook_entry_errors["reyn.yaml"] = str(exc)

    # (2) .reyn/config/hooks.yaml (the runtime IN-set — #4501's own fix).
    in_set_hooks_raw = in_set_merged.get("hooks")
    if in_set_hooks_raw:
        try:
            load_hooks(in_set_hooks_raw)
        except HookConfigError as exc:
            hook_entry_errors[".reyn/config/hooks.yaml"] = str(exc)

    # (3) every .reyn/agents/<name>/hooks.yaml (the per-agent layer).
    # load_per_agent_hooks mirrors Session._read_per_agent_hooks exactly —
    # same defensive "not a list -> []" degrade the real runtime uses, so a
    # non-list `hooks:` here is silently skipped just like it would be at an
    # actual hook-load, not a validate-only gap. Uses the SAME resolved
    # project_root as (1)/(2) above (lead-coder review, #4555).
    agents_dir = project_root / ".reyn" / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent_hooks_raw = load_per_agent_hooks(project_root, agent_dir.name)
            if not agent_hooks_raw:
                continue
            try:
                load_hooks(agent_hooks_raw)
            except HookConfigError as exc:
                hook_entry_errors[f".reyn/agents/{agent_dir.name}/hooks.yaml"] = str(exc)

    # (4) every .reyn/config/agents/<name>/hooks.yaml (#5505: the trusted
    # per-agent layer — a 4th real input path this command's own docstring
    # names, added the moment the layer exists per CLAUDE.md's "a mechanism
    # describing doc/check is stale the moment the mechanism changes"; a
    # malformed file here would otherwise only surface at the NEXT actual
    # boot, refusing it (fail-loud, #5505) with no earlier warning this
    # report-only command could have given). Same `.reyn/config/` root as
    # (2) above, per-agent-scoped like (3) — load_trusted_per_agent_hooks
    # mirrors Session._read_trusted_per_agent_hooks_raw exactly.
    trusted_agents_dir = project_root / ".reyn" / "config" / "agents"
    if trusted_agents_dir.is_dir():
        for agent_dir in sorted(trusted_agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            trusted_hooks_raw = load_trusted_per_agent_hooks(project_root, agent_dir.name)
            if not trusted_hooks_raw:
                continue
            try:
                load_hooks(trusted_hooks_raw)
            except HookConfigError as exc:
                hook_entry_errors[f".reyn/config/agents/{agent_dir.name}/hooks.yaml"] = str(exc)

    # #4631: mcp.<name> written where mcp.servers.<name> belongs loads
    # without error AND without a warning (unknown_config_keys never opens
    # `mcp:`'s own contents, same class of gap #4501 closed for hooks[]).
    # Checked PER SOURCE FILE, not the already-merged policy_merged/
    # in_set_merged dicts above — "which key is unknown" only needs the
    # merged view (#4174 T0's own question), but "which FILE is wrong"
    # needs the file identified, the same reasoning C-2's producer/consumer
    # check (merged is enough) and this check (source must be named) split
    # on, #4364. The 3 static paths + the 1 dynamic path mirror
    # `_migrate_mcp`'s own already-established scan list (this module,
    # above) exactly — not a new, narrower list.
    # #4604: the same per-source-file scan as mcp_misplaced's own loop
    # below checks a SECOND, unrelated defect too — a server entry whose
    # `type` is still the renamed-away "http" value — so both detectors
    # run off the SAME loaded raw dict per source, one file read each.
    mcp_misplaced: dict[str, "list[str]"] = {}
    mcp_renamed_http: dict[str, "list[str]"] = {}
    static_mcp_sources = {
        "~/.reyn/config.yaml": Path.home() / ".reyn" / "config.yaml",
        "reyn.yaml": project_root / "reyn.yaml",
        "reyn.local.yaml": project_root / "reyn.local.yaml",
    }
    # #5455 ②: each of these files' unknown-key check already runs above
    # (policy_unknown, on the merged policy-tier view); this second
    # read's own job is the mcp-placement/rename check, unrelated to the
    # key vocabulary.
    # #5801: same map every other project-wide-tier _load_yaml call uses.
    _mcp_diag_token_map = {"REYN_PROJECT_DIR": str(project_root)}
    for label, path in static_mcp_sources.items():
        raw = _load_yaml(
            path, vocabulary=_CheckedElsewhere.CHECKED_BY_CONFIG_VALIDATE,
            token_map=_mcp_diag_token_map,
        )
        mcp_section = raw.get("mcp")
        misplaced_found = _mcp_misplaced_server_entries(mcp_section)
        if misplaced_found:
            mcp_misplaced[label] = misplaced_found
        renamed_found = _mcp_renamed_http_transport_entries(mcp_section)
        if renamed_found:
            mcp_renamed_http[label] = renamed_found
    # #5455 ②: this one is a hot-reload IN-set file — checked at ITS OWN
    # load point (in_set_unknown, above), not the policy tier.
    dynamic_mcp_raw = _load_yaml(
        project_root / ".reyn" / "config" / "mcp.yaml",
        vocabulary=_CheckedElsewhere.CHECKED_AT_LOAD_POINT,
        token_map=_mcp_diag_token_map,
    )
    dynamic_mcp_section = dynamic_mcp_raw.get("mcp")
    dynamic_found = _mcp_misplaced_server_entries(dynamic_mcp_section)
    if dynamic_found:
        mcp_misplaced[".reyn/config/mcp.yaml"] = dynamic_found
    dynamic_renamed_found = _mcp_renamed_http_transport_entries(dynamic_mcp_section)
    if dynamic_renamed_found:
        mcp_renamed_http[".reyn/config/mcp.yaml"] = dynamic_renamed_found

    # #5455 ①: every .reyn/agents/<name>/profile.yaml, checked for
    # top-level keys that are not real AgentProfile fields — a DIFFERENT
    # operator-editable surface from the 3 above (policy tier / IN-set /
    # hooks), with its OWN closed vocabulary (dataclasses.fields, not
    # ReynConfig's schema) — see unknown_profile_keys's own docstring for
    # why this is a dedicated function, not a new entry in
    # unknown_config_keys.
    from reyn.runtime.profile import retired_profile_keys_present, unknown_profile_keys

    profile_unknown: dict[str, "frozenset[str]"] = {}
    # #5742 PR2: a SEPARATE, distinctly-reported population from
    # profile_unknown above — a retired key (with a named replacement,
    # e.g. project_context_path -> context_path) is not "unrecognized, no
    # further signal" (unknown_profile_keys's own generic bucket); folding
    # it in there would understate a finding that raises a hard error at
    # AgentProfile.load to the same severity as a stale/renamed field with
    # no effect.
    profile_retired: dict[str, "dict[str, str]"] = {}
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            profile_path = agent_dir / "profile.yaml"
            if not profile_path.is_file():
                continue
            # #5455 ②: CHECKED_BY_CALLER — the very next lines run
            # unknown_profile_keys/retired_profile_keys_present on this
            # exact return value.
            # #5801: same per-agent map AgentProfile.load itself uses —
            # this diagnostic reads profile.yaml through the same
            # structural gate, not a second, unexpanded copy of it.
            raw_profile = _load_yaml(
                profile_path, vocabulary=_CheckedElsewhere.CHECKED_BY_CALLER,
                token_map={
                    "REYN_PROJECT_DIR": str(project_root), "REYN_AGENT_NAME": agent_dir.name,
                },
            )
            retired_found = retired_profile_keys_present(raw_profile)
            if retired_found:
                profile_retired[agent_dir.name] = retired_found
            found = unknown_profile_keys(raw_profile)
            if found:
                profile_unknown[agent_dir.name] = found

    # #5455 ③: "no issues" must name what it walked — the exact defect
    # this issue was filed over (reyn config validate declaring "No
    # unknown ... config keys found" while never having looked at
    # profile.yaml at all). This tuple is the population claim; a future
    # surface added to this function must be added here too (no
    # mechanism enforces that — this is the one place that says so).
    _walked_surfaces = (
        "the policy tier (reyn.yaml / reyn.local.yaml / ~/.reyn/config.yaml)",
        "the hot-reload IN-set (.reyn/{mcp,cron,hooks,skills,pipelines,presentations}.yaml)",
        "every .reyn/agents/<name>/hooks.yaml and profile.yaml",
    )
    if (
        not policy_unknown and not disabled and not in_set_unknown
        and not hook_entry_errors and not mcp_misplaced and not mcp_renamed_http
        and not profile_unknown and not profile_retired
    ):
        print(
            "No unknown, renamed, or disabled-by-dependency config keys "
            "found. Checked: " + "; ".join(_walked_surfaces) + "."
        )
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

    if hook_entry_errors:
        if policy_unknown or disabled or in_set_unknown:
            print()
        print(
            f"Hook entry validation — checked reyn.yaml, .reyn/config/hooks.yaml, "
            f"and every .reyn/agents/<name>/hooks.yaml — "
            f"{len(hook_entry_errors)} source(s) with a malformed entry:\n"
        )
        for label, err in sorted(hook_entry_errors.items()):
            print(f"  [{label}]")
            print(f"    {err}")
        print(
            "\nEach flagged entry will fail to load the next time hooks are "
            "(re)loaded (hot-reload or restart) — fix the key(s) above."
        )

    if mcp_misplaced:
        if policy_unknown or disabled or in_set_unknown or hook_entry_errors:
            print()
        print(
            f"MCP server placement — checked ~/.reyn/config.yaml, reyn.yaml, "
            f"reyn.local.yaml, and .reyn/config/mcp.yaml — "
            f"{len(mcp_misplaced)} source(s) with a misplaced server entry:\n"
        )
        for label, names in sorted(mcp_misplaced.items()):
            for name in names:
                print(f"  [{label}] mcp.{name}")
        print(
            "\nEach name above is shaped like an MCP server entry "
            "(command/url/type) but is nested directly under mcp:, not "
            "mcp.servers: — it loads without error, but 'servers' stays "
            "empty and the server is never registered. Fix it by hand: "
            "add the missing 'servers:' key so the entry reads "
            "'mcp.servers.<name>' ('reyn config migrate-mcp' relocates "
            "already-correctly-nested mcp.servers entries between files, "
            "but does not add a missing 'servers:' key)."
        )

    if mcp_renamed_http:
        if policy_unknown or disabled or in_set_unknown or hook_entry_errors or mcp_misplaced:
            print()
        print(
            f"MCP transport type — checked ~/.reyn/config.yaml, reyn.yaml, "
            f"reyn.local.yaml, and .reyn/config/mcp.yaml — "
            f"{len(mcp_renamed_http)} source(s) with a renamed transport type:\n"
        )
        for label, names in sorted(mcp_renamed_http.items()):
            for name in names:
                print(f"  [{label}] mcp.servers.{name}.type: http")
        print(
            "\nMCP server type 'http' was renamed to 'streamable-http' "
            "(#4604), aligning reyn's own vocabulary with the Agent "
            "Plugins 1.0 canonical mcp.schema.json. Each entry above "
            "will FAIL to connect (MCPClient rejects the old value with "
            "a clear error) the next time this server is used — fix it "
            "now by hand: change 'type: http' to 'type: streamable-http' "
            "in the file named above."
        )
        # #4658: WHEN a fix takes effect differs by file — an agent (or
        # operator) that isn't told this fixes the line, retries, hits the
        # SAME error (nothing re-probed the server yet), and fixes it again
        # in a loop. Measured, not assumed: unlike this dict's other keys,
        # ~/.reyn/config.yaml / reyn.yaml / reyn.local.yaml are watched by
        # RouterHostAdapter.maybe_refresh_mcp_tools_from_yaml's own
        # independent per-turn mtime-check and self-heal on the very next
        # message — .reyn/config/mcp.yaml, despite being the general
        # hot-reload IN-set, is NOT covered by that specific watch and
        # needs an explicit `/reload` (or `reyn mcp refresh`) first.
        in_set_sources = sorted(s for s in mcp_renamed_http if s == ".reyn/config/mcp.yaml")
        policy_sources = sorted(s for s in mcp_renamed_http if s != ".reyn/config/mcp.yaml")
        if policy_sources:
            print(
                f"\n{', '.join(policy_sources)} self-heal automatically once "
                f"fixed — the corrected value is re-probed on your very next "
                f"message, no /reload and no restart needed."
            )
        if in_set_sources:
            print(
                "\n.reyn/config/mcp.yaml does not self-heal automatically — "
                "run `/reload` (or `reyn mcp refresh`) after fixing it there "
                "to apply the change without restarting."
            )

    if profile_unknown:
        if (
            policy_unknown or disabled or in_set_unknown or hook_entry_errors
            or mcp_misplaced or mcp_renamed_http
        ):
            print()
        print(
            f"Agent profile.yaml — checked every .reyn/agents/<name>/"
            f"profile.yaml — {len(profile_unknown)} agent(s) with "
            f"unrecognized key(s):\n"
        )
        for agent_name, keys in sorted(profile_unknown.items()):
            print(f"  [{agent_name}] " + ", ".join(sorted(keys)))
        print(
            "\nEach key above is not a recognized AgentProfile field "
            "('reyn config fields' does not cover this file — see "
            "reyn.runtime.profile.AgentProfile's own field list) — it is "
            "read, kept in no in-memory state, and does nothing. A field "
            "removed from AgentProfile (e.g. #5095's broker_identity) "
            "leaves this line behind in an operator's file with no "
            "further signal until now. Fix by hand: remove the key(s) "
            "above from the file named."
        )

    if profile_retired:
        # #5742 PR2: a SEPARATE section from profile_unknown above — a
        # retired key raises a HARD error at AgentProfile.load (unlike a
        # merely-unrecognized one, which is read and ignored), so this
        # report is deliberately louder and names the replacement.
        if (
            policy_unknown or disabled or in_set_unknown or hook_entry_errors
            or mcp_misplaced or mcp_renamed_http or profile_unknown
        ):
            print()
        print(
            f"Agent profile.yaml — {len(profile_retired)} agent(s) with "
            f"RETIRED key(s) (these agents cannot start until fixed):\n"
        )
        for agent_name, retired in sorted(profile_retired.items()):
            lines = ", ".join(
                f"{old!r} -> use {new!r} instead"
                for old, new in sorted(retired.items())
            )
            print(f"  [{agent_name}] {lines}")
        print(
            "\nEach key above is not merely unrecognized — AgentProfile."
            "load() raises for it, so the agent named cannot start until "
            "its profile.yaml is fixed by hand."
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

    #4375: a key in ``_REMOVED_CONFIG_KEYS`` (deleted, no successor) is
    reported in its OWN section, never folded into ``needs_manual`` above
    — lead-coder's ruling ①: a rename's remedy is "rewrite to Y"; a
    removed key's remedy is "delete it, there is no Y". Mixing the two
    into one "needs manual review" list would make that difference
    illegible (the operator would have to read each note to find out
    which action applies). Never auto-rewritten (no destination exists to
    rewrite to) and never auto-DELETED either — this command only ever
    writes a value TO a key, deleting an operator's line unasked is a
    different, more destructive class of edit this command doesn't do.
    """
    import yaml

    from reyn.config import _find_project_root
    from reyn.config.config_schema import _REMOVED_CONFIG_KEYS, _RENAMED_CONFIG_KEYS

    if not _RENAMED_CONFIG_KEYS and not _REMOVED_CONFIG_KEYS:
        print(
            "No config key renames or removals are registered yet "
            "(nothing to migrate — #4174 T1-T6 / #4375 populate these "
            "registries incrementally)."
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

    def _label(path: Path) -> str:
        try:
            return str(path.relative_to(project_root)) if project_root else str(path)
        except ValueError:
            return str(path)

    from reyn.config.migrate_check import verify_rewrite
    from reyn.config.migrate_text import rewrite_text

    _VALUE_TRANSFORM = "value transform, not a plain rename"
    _UNSUPPORTED_SHAPE = (
        "could not confidently rewrite this key's YAML in place "
        "(unsupported shape — e.g. nested source key, >1 level of nesting "
        "in the destination, or the key wasn't found in an unambiguous "
        "top-level form)"
    )
    _VERIFY_FAILED = (
        "the rewrite's own self-check disagreed with the expected result — "
        "refused rather than risk writing a wrong value"
    )

    any_changes = False
    manual_found: list[tuple[str, str, str]] = []  # (label, old_key, reason)
    removed_found: list[tuple[str, str]] = []  # (label, old_key) — #4375
    for path in candidates:
        cfg = _read(path)
        if not cfg:
            continue
        # Peek (on the PARSED structure) which auto-rewritable keys are
        # actually present in this file — decides WHAT to attempt, not
        # how to write it (that part moved to the text-level rewrite
        # below, #4295: yaml.safe_load + yaml.dump round-tripped the
        # WHOLE file through PyYAML's comment-blind loader, silently
        # dropping every operator comment on every migrate run — not
        # just on the renamed keys, on every key in the file).
        present_here = {
            old_key: new_key for old_key, new_key in auto_rewritable.items()
            if _pop_dotted(dict(cfg), old_key)[0]  # peek, don't mutate
        }
        for old_key in needs_manual:
            found, _value = _pop_dotted(dict(cfg), old_key)  # peek, don't mutate
            if found:
                manual_found.append((_label(path), old_key, _VALUE_TRANSFORM))
        # #4375: removed keys are detected and reported, never rewritten
        # (there is no destination) and never auto-deleted (this command
        # only ever writes a rewrite TO a key, not removes an operator's
        # line unasked) — see the docstring's "removed key" scope note.
        for old_key in _REMOVED_CONFIG_KEYS:
            found, _value = _pop_dotted(dict(cfg), old_key)  # peek, don't mutate
            if found:
                removed_found.append((_label(path), old_key))
        if not present_here:
            continue

        raw_text = path.read_text(encoding="utf-8")
        result = rewrite_text(raw_text, present_here)
        # Refused per-key (out of this rewriter's deliberately narrow
        # scope — see migrate_text's module docstring) fall back to
        # manual review rather than being silently skipped.
        for old_key in result.refused:
            manual_found.append((_label(path), old_key, _UNSUPPORTED_SHAPE))
        if not result.applied or result.text is None:
            continue
        applied_map = dict(result.applied)
        if not verify_rewrite(raw_text, result.text, applied_map):
            # The independent structural re-check disagrees with the
            # text-level rewrite — refuse to write a file we can't
            # confirm is value-preserving. #4295's hard requirement:
            # never silently corrupt an operator's config.
            for old_key in applied_map:
                manual_found.append((_label(path), old_key, _VERIFY_FAILED))
            continue

        any_changes = True
        print(f"{_label(path)}:")
        for old_key, new_key in result.applied:
            print(f"  {old_key} -> {new_key}")
        if not dry_run:
            path.write_text(result.text, encoding="utf-8")

    if manual_found:
        print("\nThe following renamed key(s) need manual review:")
        for label, old_key, reason in manual_found:
            note = (
                _RENAMED_CONFIG_KEYS[old_key].note
                if reason is _VALUE_TRANSFORM else reason
            )
            print(f"  {label}: {old_key} — {note}")

    if removed_found:
        if manual_found:
            print()
        # #4375, lead-coder's ruling ①: "delete", not "review" — a removed
        # key has no destination to migrate TO, so this is its own section
        # rather than folded into "needs manual review" above (that phrase
        # implies a rewrite path exists; this key has none).
        print("\nThe following key(s) were REMOVED (no successor) — delete them:")
        for label, old_key in removed_found:
            print(f"  {label}: {old_key} — {_REMOVED_CONFIG_KEYS[old_key].note}")

    if not any_changes and not manual_found and not removed_found:
        print("No renamed or removed keys found in your config — nothing to migrate.")
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
