"""`reyn init` — scaffold reyn.yaml (and optionally reyn.local.yaml) in cwd."""
from __future__ import annotations

import argparse
from pathlib import Path

from ..templates import REYN_LOCAL_CONFIG_TEMPLATE, REYN_YAML_TEMPLATE


def register(sub) -> None:
    p = sub.add_parser("init", help="Create reyn.yaml in the current directory")
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

    # reyn.local.yaml is the gitignored local override file (ADR-0031).
    # Create an example file only if neither reyn.local.yaml nor
    # reyn.local.yaml.example already exists.
    local_cfg = cwd / "reyn.local.yaml"
    local_example = cwd / "reyn.local.yaml.example"
    if local_cfg.exists():
        skipped.append("reyn.local.yaml")
    elif local_example.exists():
        skipped.append("reyn.local.yaml.example")
    else:
        local_example.write_text(REYN_LOCAL_CONFIG_TEMPLATE, encoding="utf-8")
        created.append("reyn.local.yaml.example")

    # Ensure .reyn/ (runtime state dir) exists and is gitignored.
    reyn_dir = cwd / ".reyn"
    reyn_dir.mkdir(exist_ok=True)

    gitignore = cwd / ".gitignore"
    gitignore_entries: list[str] = []
    existing_gitignore = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""

    if ".reyn/" not in existing_gitignore:
        gitignore_entries.append(".reyn/")
    if "reyn.local.yaml" not in existing_gitignore:
        gitignore_entries.append("reyn.local.yaml")

    gitignore_note = ""
    if gitignore_entries:
        separator = "\n" if existing_gitignore and not existing_gitignore.endswith("\n") else ""
        gitignore.write_text(
            existing_gitignore + separator + "\n".join(gitignore_entries) + "\n",
            encoding="utf-8",
        )
        action = "updated" if gitignore.exists() and existing_gitignore else "created"
        gitignore_note = f"  (.gitignore {action}: added {', '.join(gitignore_entries)})"

    for name in created:
        suffix = gitignore_note if name in ("reyn.local.yaml", "reyn.local.yaml.example") else ""
        print(f"  Created   {name}{suffix}")
    for name in skipped:
        print(f"  Exists    {name}  (skipped)")

    print()
    print("Next steps:")
    print("  1. Edit reyn.yaml          — set model mappings for your provider")
    print("  2. Edit reyn.local.yaml    — set api_base if using a proxy (gitignored)")
    print("     (copy from reyn.local.yaml.example if present)")
    print("  3. Export your API key:")
    print("       export OPENAI_API_KEY=sk-...")
    print("       export ANTHROPIC_API_KEY=sk-ant-...")
    print("  4. Try one of these:")
    print("       reyn chat                              # talk to the agent")
    print('       reyn run skill_builder "describe the skill you want to build"')
    print('       reyn run index_docs \'{"source":"my_docs","path":"docs/**/*.md","description":"My project documentation"}\'')
    print("       reyn chat   # then ask a question covered by your indexed docs")
    print()
    print("MCP servers (optional):")
    print("  To use stdlib skills like read_local_files, uncomment the mcp:")
    print("  block in reyn.yaml.  Full example: cookbook/configs/with-mcp.yaml")
    print("  Setup guide:          docs/guide/for-skill-authors/use-an-mcp-server.md")
