"""`reyn memory` — inspect and manage stored memories."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from reyn.data.memory.memory import (
    AmbiguousMemoryError,
    MemoryEntry,
    find_one,
    list_entries,
    render_body,
    rewrite_index,
)
from reyn.data.memory.memory_paths import memory_dir

# ── argparse wiring ───────────────────────────────────────────────────────────


_AGENT_HELP = (
    "Operate on the agent-scoped memory layer "
    "(`.reyn/agents/<name>/memory/`) instead of the shared layer "
    "(`.reyn/memory/`)."
)


def _add_layer_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", metavar="NAME", default=None, help=_AGENT_HELP)


def register(sub) -> None:
    p = sub.add_parser("memory", help="Inspect and manage chat memories")
    msub = p.add_subparsers(dest="memory_command", metavar="<subcommand>")
    msub.required = True

    p_list = msub.add_parser("list", help="List stored memories")
    _add_layer_flag(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_show = msub.add_parser("show", help="Print one memory's content")
    p_show.add_argument("name", help="Slug or memory name")
    _add_layer_flag(p_show)
    p_show.set_defaults(func=_cmd_show)

    p_edit = msub.add_parser("edit", help="Open a memory in $EDITOR")
    p_edit.add_argument("name", help="Slug or memory name")
    _add_layer_flag(p_edit)
    p_edit.set_defaults(func=_cmd_edit)

    p_del = msub.add_parser("delete", help="Delete a memory and remove it from MEMORY.md")
    p_del.add_argument("name", help="Slug or memory name")
    p_del.add_argument("--yes", "-y", action="store_true",
                       help="Skip confirmation prompt")
    _add_layer_flag(p_del)
    p_del.set_defaults(func=_cmd_delete)

    p_search = msub.add_parser("search", help="Keyword (regex) search across memories")
    p_search.add_argument("pattern", help="Regex pattern to search for")
    p_search.add_argument("--ignore-case", "-i", action="store_true")
    _add_layer_flag(p_search)
    p_search.set_defaults(func=_cmd_search)

    p_exp = msub.add_parser("export", help="Dump memories to a JSON file")
    p_exp.add_argument("--out", default="-",
                       help="Output path (default: stdout)")
    _add_layer_flag(p_exp)
    p_exp.set_defaults(func=_cmd_export)

    p_imp = msub.add_parser("import", help="Restore memories from a JSON file")
    p_imp.add_argument("file", help="JSON file produced by `reyn memory export`")
    p_imp.add_argument("--overwrite", action="store_true",
                       help="Overwrite existing memories with the same slug")
    _add_layer_flag(p_imp)
    p_imp.set_defaults(func=_cmd_import)

    p.set_defaults(func=lambda a: p.print_help())


# ── helpers ───────────────────────────────────────────────────────────────────


def _layer_dir(args: argparse.Namespace) -> Path:
    """Resolve the memory directory for the layer the user picked.

    `--agent NAME` → `.reyn/agents/NAME/memory/`. Otherwise → `.reyn/memory/`.
    Validates that the agent profile exists when `--agent` is given so we
    don't silently scan an empty directory.
    """
    agent = getattr(args, "agent", None)
    if agent is None:
        return memory_dir()
    profile_path = Path(".reyn") / "agents" / agent / "profile.yaml"
    if not profile_path.is_file():
        print(
            f"Error: agent {agent!r} not found "
            f"(expected {profile_path}). "
            f"Run `reyn agent list` to see existing agents.",
            file=sys.stderr,
        )
        sys.exit(1)
    return memory_dir(agent)


def _layer_label(args: argparse.Namespace) -> str:
    agent = getattr(args, "agent", None)
    return f"agent: {agent}" if agent else "shared"


def _entries(args: argparse.Namespace) -> list[MemoryEntry]:
    return list_entries(_layer_dir(args))


def _resolve_or_exit(args: argparse.Namespace, name: str) -> MemoryEntry:
    """Resolve a name in the chosen layer; print errors and exit if not
    found / ambiguous."""
    try:
        match = find_one(name, _entries(args))
    except AmbiguousMemoryError as exc:
        print(
            f"Multiple memories match {exc.query!r} in {_layer_label(args)}:",
            file=sys.stderr,
        )
        for e in exc.matches:
            print(f"  {e.slug}  ({e.name})", file=sys.stderr)
        print("Pass the exact slug to disambiguate.", file=sys.stderr)
        sys.exit(1)
    if match is None:
        print(
            f"No memory matching {name!r} in {_layer_label(args)}.",
            file=sys.stderr,
        )
        sys.exit(1)
    return match


# ── command handlers ─────────────────────────────────────────────────────────


def _cmd_list(args: argparse.Namespace) -> None:
    layer_dir = _layer_dir(args)
    entries = _entries(args)
    if not entries:
        print(f"No memories found in {_layer_label(args)} layer ({layer_dir}).")
        return
    print(f"\n[{_layer_label(args)}] {layer_dir}")
    for e in entries:
        type_str = f"[{e.type}]" if e.type else "[?]"
        desc = f"  — {e.description}" if e.description else ""
        print(f"  {e.slug}  {type_str} {e.name}{desc}")
    print()


def _cmd_show(args: argparse.Namespace) -> None:
    e = _resolve_or_exit(args, args.name)
    print(f"# {e.name}  [{e.type}]")
    print(f"# slug: {e.slug}")
    print(f"# path: {e.path}")
    print(f"# layer: {_layer_label(args)}")
    if e.description:
        print(f"# description: {e.description}")
    print()
    print(e.body)


def _cmd_edit(args: argparse.Namespace) -> None:
    e = _resolve_or_exit(args, args.name)
    editor = os.environ.get("EDITOR") or "vi"
    if not shutil.which(editor.split()[0]):
        print(f"Error: editor {editor!r} not found. Set $EDITOR.", file=sys.stderr)
        sys.exit(1)
    rc = subprocess.call([*editor.split(), str(e.path)])
    if rc != 0:
        print(f"Editor exited with status {rc}; index not refreshed.", file=sys.stderr)
        sys.exit(rc)
    rewrite_index(e.path.parent)
    print(f"Saved {e.path}; MEMORY.md refreshed.")


def _cmd_delete(args: argparse.Namespace) -> None:
    e = _resolve_or_exit(args, args.name)
    if not args.yes:
        try:
            ans = input(f"Delete {e.path} ? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if ans != "y":
            print("Aborted.")
            return
    e.path.unlink()
    rewrite_index(e.path.parent)
    print(f"Deleted {e.slug} from {_layer_label(args)} layer.")


def _cmd_search(args: argparse.Namespace) -> None:
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        regex = re.compile(args.pattern, flags)
    except re.error as exc:
        print(f"Invalid regex: {exc}", file=sys.stderr)
        sys.exit(1)
    hit_count = 0
    for e in _entries(args):
        haystack = f"{e.name}\n{e.description}\n{e.body}"
        if not regex.search(haystack):
            continue
        hit_count += 1
        print(f"\n{e.slug}  ({e.name})  [{e.type}]")
        for i, line in enumerate(e.body.splitlines(), start=1):
            if regex.search(line):
                print(f"  {i:3d}: {line}")
    if hit_count == 0:
        print(
            f"No memories matched {args.pattern!r} in {_layer_label(args)} layer."
        )
    else:
        print(f"\n{hit_count} memor{'ies' if hit_count != 1 else 'y'} matched.")


def _cmd_export(args: argparse.Namespace) -> None:
    entries = _entries(args)
    payload = {
        "version": 1,
        "layer": _layer_label(args),
        "entries": [
            {
                "slug": e.slug, "name": e.name,
                "description": e.description, "type": e.type, "body": e.body,
            }
            for e in entries
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Exported {len(entries)} memories → {args.out}")


def _cmd_import(args: argparse.Namespace) -> None:
    raw = Path(args.file).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    entries = payload.get("entries") or []
    if not isinstance(entries, list):
        print("Invalid format: 'entries' must be a list.", file=sys.stderr)
        sys.exit(1)

    target_dir = _layer_dir(args)
    target_dir.mkdir(parents=True, exist_ok=True)

    skipped = 0
    written = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug") or ""
        if not slug:
            continue
        target = target_dir / f"{slug}.md"
        if target.exists() and not args.overwrite:
            print(f"  skip (exists): {slug}", file=sys.stderr)
            skipped += 1
            continue
        target.write_text(
            render_body(
                name=entry.get("name") or slug,
                description=entry.get("description") or "",
                type_=entry.get("type") or "",
                body=entry.get("body") or "",
            ),
            encoding="utf-8",
        )
        written += 1

    if written:
        rewrite_index(target_dir)

    print(
        f"Imported {written} memories into {_layer_label(args)} layer "
        f"({skipped} skipped)."
    )
