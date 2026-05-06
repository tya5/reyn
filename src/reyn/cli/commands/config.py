"""`reyn config` — view and edit reyn configuration."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reyn.config import load_config

from ..templates import CONFIG_FIELDS


def register(sub) -> None:
    p = sub.add_parser("config", help="View and edit reyn configuration")
    csub = p.add_subparsers(dest="config_cmd", metavar="<subcommand>")
    p.set_defaults(func=run)

    csub.add_parser("show", help="Show current effective config (merged from all sources)")
    csub.add_parser("fields", help="List all config fields with descriptions and examples")

    g = csub.add_parser("get", help="Get a single config value")
    g.add_argument("key", metavar="KEY", help="Config key (e.g. model, api_base)")

    s = csub.add_parser("set", help="Set a config value in .reyn/config.yaml")
    s.add_argument("key", metavar="KEY",
                   help="Config key (e.g. api_base, models.standard). Run 'reyn config fields' for the full list.")
    s.add_argument("value", metavar="VALUE", help="Value to set (YAML syntax accepted)")


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
    else:
        _show()


def _fields() -> None:
    W_KEY, W_DEF, W_SCOPE = 18, 10, 34
    header = f"{'Field':<{W_KEY}}  {'Default':<{W_DEF}}  {'Scope':<{W_SCOPE}}  Description"
    print(header)
    print("─" * len(header))
    for f in CONFIG_FIELDS:
        print(f"{f['key']:<{W_KEY}}  {f['default']:<{W_DEF}}  {f['scope']:<{W_SCOPE}}  {f['desc']}")
        print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}  Values:  {f['values']}")
        print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}  Example: {f['example'].splitlines()[0]}")
        for extra_line in f['example'].splitlines()[1:]:
            print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}           {extra_line}")
        print()


def _show() -> None:
    import yaml
    config = load_config()
    effective = {
        "model":           config.model,
        "models":          config.models,
        "api_base":        config.api_base or "(not set)",
        "output_language": config.output_language or "(not set — chat router skips language directive; phase paths default to ja)",
        "shell_allowed":   config.shell_allowed,
        "permissions":     config.permissions,
        "mcp":             config.mcp if config.mcp else "(not configured)",
    }
    print("# Effective config (merged from all sources)")
    print(yaml.dump(effective, allow_unicode=True, default_flow_style=False), end="")


def _get(key: str) -> None:
    import yaml
    config = load_config()
    value = getattr(config, key, None)
    if value is None:
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        print("Run 'reyn config fields' to see available keys.", file=sys.stderr)
        sys.exit(1)
    if isinstance(value, (dict, list)):
        print(yaml.dump(value, allow_unicode=True, default_flow_style=False), end="")
    else:
        print(value)


def _set(key: str, value: str) -> None:
    import yaml
    valid_keys = {f["key"] for f in CONFIG_FIELDS}
    check_key = key.split(".")[0] if "." in key else key
    if check_key not in valid_keys:
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        print("Run 'reyn config fields' to see available keys.", file=sys.stderr)
        sys.exit(1)

    local_cfg = Path(".reyn") / "config.yaml"
    local_cfg.parent.mkdir(exist_ok=True)
    current: dict = {}
    if local_cfg.exists():
        current = yaml.safe_load(local_cfg.read_text(encoding="utf-8")) or {}

    try:
        parsed = yaml.safe_load(value)
    except Exception:
        parsed = value

    if "." in key:
        parent, child = key.split(".", 1)
        current.setdefault(parent, {})[child] = parsed
    else:
        current[key] = parsed

    local_cfg.write_text(yaml.dump(current, allow_unicode=True, default_flow_style=False),
                         encoding="utf-8")
    print(f"Set {key} = {parsed!r}  →  {local_cfg}")
