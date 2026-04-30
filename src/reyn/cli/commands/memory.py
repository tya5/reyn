"""`reyn memory` — inspect and manage stored memories."""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from reyn.compiler.parser import _split_frontmatter
from reyn.memory_paths import global_memory_dir, project_memory_dir

from ..session import Session


@dataclass
class _MemoryEntry:
    scope: str           # "global" | "project"
    slug: str            # filename without .md
    path: Path           # absolute path to .md
    name: str            # frontmatter name
    description: str     # frontmatter description
    type: str            # frontmatter type
    body: str            # body text (after frontmatter)


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

    p.set_defaults(func=lambda a: _print_help(p))


def _add_scope_arg(parser: argparse.ArgumentParser, default: str = "project") -> None:
    parser.add_argument(
        "--scope", choices=["global", "project", "all"], default=default,
        help=f"Memory scope to operate on (default: {default})",
    )


def _print_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()


# ── helpers ───────────────────────────────────────────────────────────────────


def _scope_dirs(scope: str) -> list[tuple[str, Path]]:
    """Return [(scope_label, dir_path)] for the requested scope filter."""
    sess = Session.from_args(argparse.Namespace())
    state_dir = sess.config.state_dir
    g = global_memory_dir()
    p = project_memory_dir(state_dir)
    if scope == "global":
        return [("global", g)]
    if scope == "project":
        return [("project", p)]
    return [("global", g), ("project", p)]


def _read_entry(scope: str, path: Path) -> _MemoryEntry | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = _split_frontmatter(text)
    return _MemoryEntry(
        scope=scope,
        slug=path.stem,
        path=path,
        name=str(fm.get("name") or path.stem),
        description=str(fm.get("description") or "").strip().splitlines()[0]
        if fm.get("description") else "",
        type=str(fm.get("type") or ""),
        body=body.strip(),
    )


def _all_entries(scope: str) -> list[_MemoryEntry]:
    entries: list[_MemoryEntry] = []
    for label, d in _scope_dirs(scope):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            e = _read_entry(label, f)
            if e is not None:
                entries.append(e)
    return entries


def _resolve_one(name: str, scope: str) -> _MemoryEntry:
    """Find a single memory matching `name` (slug or display name).

    Prints a useful error and exits 1 if zero or multiple matches.
    """
    candidates = _all_entries(scope)
    target = name.strip()
    if target.endswith(".md"):
        target = target[:-3]

    # First: exact slug match
    exact_slug = [e for e in candidates if e.slug == target]
    if len(exact_slug) == 1:
        return exact_slug[0]
    if len(exact_slug) > 1:
        _ambiguous_exit(target, exact_slug)

    # Second: case-insensitive name match
    ci_name = [e for e in candidates if e.name.lower() == target.lower()]
    if len(ci_name) == 1:
        return ci_name[0]
    if len(ci_name) > 1:
        _ambiguous_exit(target, ci_name)

    # Third: substring match on slug or name
    sub = [e for e in candidates
           if target.lower() in e.slug.lower() or target.lower() in e.name.lower()]
    if len(sub) == 1:
        return sub[0]
    if len(sub) > 1:
        _ambiguous_exit(target, sub)

    print(f"No memory matching {name!r} in scope={scope}.", file=sys.stderr)
    sys.exit(1)


def _ambiguous_exit(query: str, matches: list[_MemoryEntry]) -> None:
    print(f"Multiple memories match {query!r}:", file=sys.stderr)
    for e in matches:
        print(f"  [{e.scope}] {e.slug}  ({e.name})", file=sys.stderr)
    print("Use --scope to disambiguate, or pass the exact slug.", file=sys.stderr)
    sys.exit(1)


def _rewrite_index(scope_dir: Path) -> None:
    """Rebuild MEMORY.md from the .md files in scope_dir."""
    entries: list[_MemoryEntry] = []
    for f in sorted(scope_dir.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        e = _read_entry("?", f)
        if e is not None:
            entries.append(e)
    lines = ["# Memory Index", ""]
    for e in entries:
        desc = f" — {e.description}" if e.description else ""
        lines.append(f"- [{e.name}]({e.slug}.md){desc}")
    index_path = scope_dir / "MEMORY.md"
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── command handlers ─────────────────────────────────────────────────────────


def _cmd_list(args: argparse.Namespace) -> None:
    entries = _all_entries(args.scope)
    if not entries:
        print(f"No memories in scope={args.scope}.")
        return
    cur_scope = None
    for e in entries:
        if e.scope != cur_scope:
            cur_scope = e.scope
            label = "global" if e.scope == "global" else "project"
            d = (global_memory_dir() if e.scope == "global"
                 else project_memory_dir(Session.from_args(argparse.Namespace()).config.state_dir))
            print(f"\n{label}  ({d})")
        type_str = f"[{e.type}]" if e.type else "[?]"
        desc = f"  — {e.description}" if e.description else ""
        print(f"  {e.slug}  {type_str} {e.name}{desc}")
    print()


def _cmd_show(args: argparse.Namespace) -> None:
    e = _resolve_one(args.name, args.scope)
    print(f"# {e.name}  [{e.scope}/{e.type}]")
    print(f"# slug: {e.slug}")
    print(f"# path: {e.path}")
    if e.description:
        print(f"# description: {e.description}")
    print()
    print(e.body)


def _cmd_edit(args: argparse.Namespace) -> None:
    e = _resolve_one(args.name, args.scope)
    editor = os.environ.get("EDITOR") or "vi"
    if not shutil.which(editor.split()[0]):
        print(f"Error: editor {editor!r} not found. Set $EDITOR.", file=sys.stderr)
        sys.exit(1)
    rc = subprocess.call([*editor.split(), str(e.path)])
    if rc != 0:
        print(f"Editor exited with status {rc}; index not refreshed.", file=sys.stderr)
        sys.exit(rc)
    _rewrite_index(e.path.parent)
    print(f"Saved {e.path}; MEMORY.md refreshed.")


def _cmd_delete(args: argparse.Namespace) -> None:
    e = _resolve_one(args.name, args.scope)
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
    _rewrite_index(e.path.parent)
    print(f"Deleted {e.slug} from {e.scope}.")


def _cmd_search(args: argparse.Namespace) -> None:
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        regex = re.compile(args.pattern, flags)
    except re.error as exc:
        print(f"Invalid regex: {exc}", file=sys.stderr)
        sys.exit(1)
    entries = _all_entries(args.scope)
    hit_count = 0
    for e in entries:
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
    entries = _all_entries(args.scope)
    payload = {
        "version": 1,
        "entries": [
            {
                "scope": e.scope,
                "slug": e.slug,
                "name": e.name,
                "description": e.description,
                "type": e.type,
                "body": e.body,
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

    sess = Session.from_args(argparse.Namespace())
    g = global_memory_dir()
    p = project_memory_dir(sess.config.state_dir)
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
        name = entry.get("name") or slug
        body = entry.get("body") or ""
        type_ = entry.get("type") or ""
        description = entry.get("description") or ""
        if not slug:
            continue
        target_dir = g if scope == "global" else p
        target = target_dir / f"{slug}.md"
        if target.exists() and not args.overwrite:
            print(f"  skip (exists): {scope}/{slug}", file=sys.stderr)
            skipped += 1
            continue
        text = (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {type_}\n"
            "---\n\n"
            f"{body.strip()}\n"
        )
        target.write_text(text, encoding="utf-8")
        touched_dirs.add(target_dir)
        written += 1

    for d in touched_dirs:
        _rewrite_index(d)

    print(f"Imported {written} memories ({skipped} skipped).")
