"""`reyn init` — scaffold reyn.yaml and .reyn/config.yaml in cwd."""
from __future__ import annotations

import argparse
from pathlib import Path

from ..templates import REYN_LOCAL_CONFIG_TEMPLATE, REYN_YAML_TEMPLATE


def register(sub) -> None:
    p = sub.add_parser("init", help="Create reyn.yaml and .reyn/config.yaml in the current directory")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    cwd = Path.cwd()
    created: list[str] = []
    skipped: list[str] = []

    project_cfg = cwd / "reyn.yaml"
    if project_cfg.exists():
        skipped.append("reyn.yaml")
    else:
        project_cfg.write_text(REYN_YAML_TEMPLATE, encoding="utf-8")
        created.append("reyn.yaml")

    reyn_dir = cwd / ".reyn"
    reyn_dir.mkdir(exist_ok=True)
    local_cfg = reyn_dir / "config.yaml"
    if local_cfg.exists():
        skipped.append(".reyn/config.yaml")
    else:
        local_cfg.write_text(REYN_LOCAL_CONFIG_TEMPLATE, encoding="utf-8")
        created.append(".reyn/config.yaml")

    gitignore = cwd / ".gitignore"
    gitignore_note = ""
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".reyn/" not in content:
            gitignore.write_text(content.rstrip() + "\n.reyn/\n", encoding="utf-8")
            gitignore_note = "  (.gitignore updated)"
    else:
        gitignore.write_text(".reyn/\n", encoding="utf-8")
        gitignore_note = "  (.gitignore created)"

    for name in created:
        suffix = gitignore_note if name == ".reyn/config.yaml" else ""
        print(f"  Created   {name}{suffix}")
    for name in skipped:
        print(f"  Exists    {name}  (skipped)")

    print()
    print("Next steps:")
    print("  1. Edit reyn.yaml         — set model mappings for your provider")
    print("  2. Edit .reyn/config.yaml — set api_base if using a proxy")
    print("  3. Export your API key:")
    print("       export OPENAI_API_KEY=sk-...")
    print("       export ANTHROPIC_API_KEY=sk-ant-...")
    print("  4. Run an app:")
    print('       reyn run app_builder "describe the app you want to build"')
    print()
    print("MCP servers (optional):")
    print("  To use stdlib skills like read_local_files, uncomment the mcp:")
    print("  block in reyn.yaml.  Full example: cookbook/configs/with-mcp.yaml")
    print("  Setup guide:          docs/en/how-to/use-an-mcp-server.md")
