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

    p_list = psub.add_parser("list", help="Show all saved approvals")
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

# #5153 (architect ruling, issuecomment-5383838646, scope confirmed
# issuecomment-5383848849): this command used to read/write
# `.reyn/approvals.yaml` directly with its own `_load`/`_save`, a THIRD
# independent writer alongside `PermissionResolver._persist` and the web
# router's own equivalent — all 3 racing the same snapshot
# read-modify-write. This surface, with no live `PermissionResolver`
# instance (a standalone CLI invocation, often with no `reyn` process
# running at all), is exactly why "make one process own the file" was
# rejected as a fix (architect: "server 単一 writer は server 無し CLI が
# 死ぬ"). `ApprovalLedger` needs no live resolver — constructing one here
# is the same shape a running session uses.


def _ledger_path() -> Path:
    # #5173: derived from the SAME constant PermissionResolver and the #1199
    # write-gate carve-out use — a re-typed literal here is a 4th copy of the
    # live path that could silently drift from the two the carve-out actually
    # checks (exactly the class of gap #5173 found).
    from reyn.security.permissions.approval_ledger import RELATIVE_PATH

    project_root = _find_project_root(Path.cwd()) or Path.cwd()
    return project_root / Path(RELATIVE_PATH)


def _legacy_snapshot_path() -> Path:
    project_root = _find_project_root(Path.cwd()) or Path.cwd()
    return project_root / ".reyn" / "approvals.yaml"


def _load() -> dict[str, bool]:
    """Fold the ledger (migrating a legacy snapshot first, if present) —
    the SAME read every other approvals surface (`PermissionResolver`,
    the web router) now does."""
    from reyn.security.permissions.approval_ledger import (
        ApprovalLedger,
        migrate_legacy_snapshot,
    )
    ledger = ApprovalLedger(_ledger_path())
    migrate_legacy_snapshot(ledger, _legacy_snapshot_path())
    approvals, _bound = ledger.fold()
    return approvals


def _parse_key(key: str) -> tuple[str, str, str] | None:
    """Split `<actor>/<kind>/<path-or-dir>` into its parts.

    `kind` is `file.read` or `file.write` (contains a dot, takes 2 segments).
    Returns (actor, kind, path) or None for keys that don't match the file
    pattern (e.g. `mcp.<server>` for MCP approvals).
    """
    parts = key.split("/", 3)
    # Expect at least: actor / file.read|file.write / path
    if len(parts) < 3:
        return None
    actor = parts[0]
    kind = parts[1]
    path = "/".join(parts[2:])
    if kind not in ("file.read", "file.write"):
        return None
    return actor, kind, path


# ── handlers ─────────────────────────────────────────────────────────────────


def _cmd_list(args: argparse.Namespace) -> None:
    path = _ledger_path()
    data = _load()
    if not data:
        print(f"No saved approvals at {path}.")
        return
    print(f"# {path}")
    print()
    file_keys: list[tuple[str, str, str, bool]] = []  # actor, kind, path, approved
    other_keys: list[tuple[str, bool]] = []
    for key, approved in data.items():
        parsed = _parse_key(key)
        if parsed is None:
            other_keys.append((key, approved))
        else:
            actor, kind, p = parsed
            file_keys.append((actor, kind, p, approved))

    if file_keys:
        # Group by actor, then by kind, for readability
        file_keys.sort(key=lambda x: (x[0], x[1], x[2]))
        cur_actor = None
        for actor, kind, p, approved in file_keys:
            if actor != cur_actor:
                cur_actor = actor
                print(f"  [{actor}]")
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
    from reyn.security.permissions.approval_ledger import ApprovalLedger
    ApprovalLedger(_ledger_path()).append_approval(args.key, False)
    print(f"Revoked {args.key!r}.")


def _cmd_clear(args: argparse.Namespace) -> None:
    data = _load()
    currently_approved = {k for k, v in data.items() if v}
    if not currently_approved:
        print("No saved approvals to clear.")
        return
    if not args.yes:
        try:
            ans = input(
                f"Remove all {len(currently_approved)} approvals from "
                f"{_ledger_path()} ? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if ans != "y":
            print("Aborted.")
            return
    from reyn.security.permissions.approval_ledger import ApprovalLedger
    ledger = ApprovalLedger(_ledger_path())
    for key in currently_approved:
        ledger.append_approval(key, False)
    print(f"Cleared {len(currently_approved)} approval(s).")
