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

from reyn.memory import (
    AmbiguousMemoryError,
    MemoryEntry,
    default_scope_dirs,
    find_one,
    list_entries,
    render_body,
    rewrite_index,
)
from reyn.memory_paths import global_memory_dir, project_memory_dir

from ..session import Session


# ── argparse wiring ───────────────────────────────────────────────────────────


def register(sub) -> None:
    p = sub.add_parser("memory", help="Inspect and manage chat memories")
    msub = p.add_subparsers(dest="memory_command", metavar="<subcommand>")
    msub.required = True

    p_list = msub.add_parser("list", help="List stored memories")
    _add_scope_arg(p_list, default="all")
    p_list.set_defaults(func=_cmd_list)

    p_show = msub.add_parser("show", help="Print one memory's content")
    p_show.add_argument("name", help="Slug or memory name")
    _add_scope_arg(p_show)
    p_show.set_defaults(func=_cmd_show)

    p_edit = msub.add_parser("edit", help="Open a memory in $EDITOR")
    p_edit.add_argument("name", help="Slug or memory name")
    _add_scope_arg(p_edit)
    p_edit.set_defaults(func=_cmd_edit)

    p_del = msub.add_parser("delete", help="Delete a memory and remove it from MEMORY.md")
    p_del.add_argument("name", help="Slug or memory name")
    _add_scope_arg(p_del)
    p_del.add_argument("--yes", "-y", action="store_true",
                       help="Skip confirmation prompt")
    p_del.set_defaults(func=_cmd_delete)

    p_search = msub.add_parser("search", help="Keyword (regex) search across memories")
    p_search.add_argument("pattern", help="Regex pattern to search for")
    _add_scope_arg(p_search, default="all")
    p_search.add_argument("--ignore-case", "-i", action="store_true")
    p_search.set_defaults(func=_cmd_search)

    p_exp = msub.add_parser("export", help="Dump memories to a JSON file")
    _add_scope_arg(p_exp, default="all")
    p_exp.add_argument("--out", default="-",
                       help="Output path (default: stdout)")
    p_exp.set_defaults(func=_cmd_export)

    p_imp = msub.add_parser("import", help="Restore memories from a JSON file")
    p_imp.add_argument("file", help="JSON file produced by `reyn memory export`")
    p_imp.add_argument(
        "--scope", choices=["global", "project"],
        help="Override scope for all imported entries (default: use entry's own scope)",
    )
    p_imp.add_argument("--overwrite", action="store_true",
                       help="Overwrite existing memories with the same slug")
    p_imp.set_defaults(func=_cmd_import)

    p.set_defaults(func=lambda a: p.print_help())


def _add_scope_arg(parser: argparse.ArgumentParser, default: str = "project") -> None:
    parser.add_argument(
        "--scope", choices=["global", "project", "all"], default=default,
        help=f"Memory scope to operate on (default: {default})",
    )


# ── helpers ───────────────────────────────────────────────────────────────────


def _state_dir() -> str:
    """Resolve state_dir from the merged config without needing parsed CLI args."""
    return Session.from_args(argparse.Namespace()).config.state_dir


def _scope_dirs(scope: str) -> list[tuple[str, Path]]:
    """Filter the canonical scope_dir pair down to the requested scope."""
    pairs = default_scope_dirs(_state_dir())
    if scope == "all":
        return pairs
    return [(label, d) for label, d in pairs if label == scope]


def _entries(scope: str) -> list[MemoryEntry]:
    return list_entries(_scope_dirs(scope))


def _resolve_or_exit(name: str, scope: str) -> MemoryEntry:
    """Resolve a name; print errors and exit if not found / ambiguous."""
    try:
        match = find_one(name, _entries(scope))
    except AmbiguousMemoryError as exc:
        print(f"Multiple memories match {exc.query!r}:", file=sys.stderr)
        for e in exc.matches:
            print(f"  [{e.scope}] {e.slug}  ({e.name})", file=sys.stderr)
        print("Use --scope to disambiguate, or pass the exact slug.", file=sys.stderr)
        sys.exit(1)
    if match is None:
        print(f"No memory matching {name!r} in scope={scope}.", file=sys.stderr)
        sys.exit(1)
    return match


# ── command handlers ─────────────────────────────────────────────────────────


def _cmd_list(args: argparse.Namespace) -> None:
    entries = _entries(args.scope)
    if not entries:
        print(f"No memories in scope={args.scope}.")
        return
    cur_scope = None
    for e in entries:
        if e.scope != cur_scope:
            cur_scope = e.scope
            d = (global_memory_dir() if e.scope == "global"
                 else project_memory_dir(_state_dir()))
            print(f"\n{e.scope}  ({d})")
        type_str = f"[{e.type}]" if e.type else "[?]"
        desc = f"  — {e.description}" if e.description else ""
        print(f"  {e.slug}  {type_str} {e.name}{desc}")
    print()


def _cmd_show(args: argparse.Namespace) -> None:
    e = _resolve_or_exit(args.name, args.scope)
    print(f"# {e.name}  [{e.scope}/{e.type}]")
    print(f"# slug: {e.slug}")
    print(f"# path: {e.path}")
    if e.description:
        print(f"# description: {e.description}")
    print()
    print(e.body)


def _cmd_edit(args: argparse.Namespace) -> None:
    e = _resolve_or_exit(args.name, args.scope)
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
    e = _resolve_or_exit(args.name, args.scope)
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
    print(f"Deleted {e.slug} from {e.scope}.")


def _cmd_search(args: argparse.Namespace) -> None:
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        regex = re.compile(args.pattern, flags)
    except re.error as exc:
        print(f"Invalid regex: {exc}", file=sys.stderr)
        sys.exit(1)
    hit_count = 0
    for e in _entries(args.scope):
        haystack = f"{e.name}\n{e.description}\n{e.body}"
        if not regex.search(haystack):
            continue
        hit_count += 1
        print(f"\n[{e.scope}] {e.slug}  ({e.name})  [{e.type}]")
        for i, line in enumerate(e.body.splitlines(), start=1):
            if regex.search(line):
                print(f"  {i:3d}: {line}")
    if hit_count == 0:
        print(f"No memories matched {args.pattern!r}.")
    else:
        print(f"\n{hit_count} memor{'ies' if hit_count != 1 else 'y'} matched.")


def _cmd_export(args: argparse.Namespace) -> None:
    entries = _entries(args.scope)
    payload = {
        "version": 1,
        "entries": [
            {
                "scope": e.scope, "slug": e.slug, "name": e.name,
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

    state_dir = _state_dir()
    g = global_memory_dir()
    p = project_memory_dir(state_dir)
    g.mkdir(parents=True, exist_ok=True)
    p.mkdir(parents=True, exist_ok=True)

    touched_dirs: set[Path] = set()
    skipped = 0
    written = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scope = args.scope or entry.get("scope") or "project"
        slug = entry.get("slug") or ""
        if not slug:
            continue
        target_dir = g if scope == "global" else p
        target = target_dir / f"{slug}.md"
        if target.exists() and not args.overwrite:
            print(f"  skip (exists): {scope}/{slug}", file=sys.stderr)
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
        touched_dirs.add(target_dir)
        written += 1

    for d in touched_dirs:
        rewrite_index(d)

    print(f"Imported {written} memories ({skipped} skipped).")
