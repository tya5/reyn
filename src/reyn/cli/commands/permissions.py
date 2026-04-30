"""`reyn permissions` — inspect and revoke saved permission approvals."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from reyn.config import _find_project_root


# ── argparse ─────────────────────────────────────────────────────────────────


def register(sub) -> None:
    p = sub.add_parser("permissions", help="Inspect and revoke saved permission approvals")
    psub = p.add_subparsers(dest="permissions_command", metavar="<subcommand>")
    psub.required = True

    p_list = psub.add_parser("list", help="Show all saved approvals from .reyn/approvals.yaml")
    p_list.set_defaults(func=_cmd_list)

    p_revoke = psub.add_parser("revoke", help="Remove a single approval entry by key")
    p_revoke.add_argument("key", help="The approval key to revoke (see `reyn permissions list`)")
    p_revoke.set_defaults(func=_cmd_revoke)

    p_clear = psub.add_parser("clear", help="Remove all saved approvals")
    p_clear.add_argument("--yes", "-y", action="store_true",
                         help="Skip confirmation prompt")
    p_clear.set_defaults(func=_cmd_clear)

    p.set_defaults(func=lambda a: p.print_help())


# ── helpers ──────────────────────────────────────────────────────────────────


def _approvals_path() -> Path:
    project_root = _find_project_root(Path.cwd()) or Path.cwd()
    return project_root / ".reyn" / "approvals.yaml"


def _load() -> dict[str, bool]:
    path = _approvals_path()
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"Failed to parse {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        return {}
    return {str(k): bool(v) for k, v in data.items() if isinstance(v, bool)}


def _save(data: dict[str, bool]) -> None:
    import yaml
    path = _approvals_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        # Keep the file present but empty so tooling/diff stays predictable.
        path.write_text("{}\n", encoding="utf-8")
        return
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _parse_key(key: str) -> tuple[str, str, str] | None:
    """Split `<skill>/<kind>/<path-or-dir>` into its parts.

    `kind` is `file.read` or `file.write` (contains a dot, takes 2 segments).
    Returns (skill, kind, path) or None for keys that don't match the file
    pattern (e.g. `mcp.<server>` for MCP approvals).
    """
    parts = key.split("/", 3)
    # Expect at least: skill / file.read|file.write / path
    if len(parts) < 3:
        return None
    skill = parts[0]
    kind = parts[1]
    path = "/".join(parts[2:])
    if kind not in ("file.read", "file.write"):
        return None
    return skill, kind, path


# ── handlers ─────────────────────────────────────────────────────────────────


def _cmd_list(args: argparse.Namespace) -> None:
    path = _approvals_path()
    data = _load()
    if not data:
        print(f"No saved approvals at {path}.")
        return
    print(f"# {path}")
    print()
    file_keys: list[tuple[str, str, str, bool]] = []  # skill, kind, path, approved
    other_keys: list[tuple[str, bool]] = []
    for key, approved in data.items():
        parsed = _parse_key(key)
        if parsed is None:
            other_keys.append((key, approved))
        else:
            skill, kind, p = parsed
            file_keys.append((skill, kind, p, approved))

    if file_keys:
        # Group by skill, then by kind, for readability
        file_keys.sort(key=lambda x: (x[0], x[1], x[2]))
        cur_skill = None
        for skill, kind, p, approved in file_keys:
            if skill != cur_skill:
                cur_skill = skill
                print(f"  [{skill}]")
            verb = "read " if kind == "file.read" else "write"
            scope = "recursive" if p.endswith("/") else "just_path"
            mark = "✓" if approved else "✗"
            print(f"    {mark} {verb}  {p}  ({scope})")
        print()

    if other_keys:
        print("  [other]")
        for key, approved in other_keys:
            mark = "✓" if approved else "✗"
            print(f"    {mark} {key}")
        print()

    print(f"Total: {len(data)} entries")
    print("Use `reyn permissions revoke <key>` to remove one.")


def _cmd_revoke(args: argparse.Namespace) -> None:
    data = _load()
    if args.key not in data:
        print(f"No saved approval with key {args.key!r}.", file=sys.stderr)
        # Friendly suggestion: any partial matches?
        hits = [k for k in data if args.key in k]
        if hits:
            print("Did you mean one of:", file=sys.stderr)
            for k in hits[:5]:
                print(f"  {k}", file=sys.stderr)
        sys.exit(1)
    del data[args.key]
    _save(data)
    print(f"Revoked {args.key!r}.")


def _cmd_clear(args: argparse.Namespace) -> None:
    data = _load()
    if not data:
        print("No saved approvals to clear.")
        return
    if not args.yes:
        try:
            ans = input(f"Remove all {len(data)} approvals from {_approvals_path()} ? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if ans != "y":
            print("Aborted.")
            return
    _save({})
    print(f"Cleared {len(data)} approval(s).")
