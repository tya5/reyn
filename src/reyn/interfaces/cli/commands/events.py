"""`reyn events` — replay and manage saved event JSONL files (PR20).

Forms:
  reyn events <PATH>                  # replay events from a file or directory
  reyn events purge --before <DATE>   # delete event files older than a date
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..logger_factory import make_logger

_FILENAME_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T(\d{6}))?")


def register(sub) -> None:
    p = sub.add_parser("events", help="Replay or manage saved event JSONL files")
    p.add_argument(
        "target", metavar="PATH-OR-PURGE", nargs="?",
        help=(
            "Path to a .jsonl file or a directory under .reyn/events/, "
            "or the literal 'purge' to delete old files (use --before)."
        ),
    )
    p.add_argument(
        "--filter", metavar="TYPE", action="append", dest="filter_types", default=[],
        help="Only show events of this type (repeatable)",
    )
    p.add_argument(
        "--skip", metavar="TYPE", action="append", dest="skip_types", default=[],
        help="Skip events of this type (repeatable)",
    )
    p.add_argument(
        "--conversation", action="store_true",
        help=(
            "Show LLM conversation history: display context frames sent to "
            "the LLM and the raw responses received, in order. Overrides "
            "--filter and --skip."
        ),
    )
    p.add_argument(
        "--since", metavar="YYYY-MM-DD", default=None,
        help="Skip files / events before this date (inclusive)",
    )
    p.add_argument(
        "--until", metavar="YYYY-MM-DD", default=None,
        help="Skip files / events after this date (inclusive)",
    )
    p.add_argument(
        "--before", metavar="YYYY-MM-DD", default=None,
        help="(purge mode) Delete files whose start-date is strictly before this date",
    )
    p.add_argument(
        "--agent", metavar="NAME", default=None,
        help="(purge mode) Limit purge to .reyn/events/agents/<NAME>/",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="(purge mode) Print files that would be deleted, don't delete")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    target = args.target
    if target is None:
        print(
            "Error: target is required.\n"
            "  reyn events <PATH>           replay events\n"
            "  reyn events purge --before <YYYY-MM-DD>   delete old files",
            file=sys.stderr,
        )
        sys.exit(1)

    if target == "purge":
        return run_purge(args)
    return run_replay(args)


# ── replay ───────────────────────────────────────────────────────────────────


def run_replay(args: argparse.Namespace) -> None:
    from reyn.schemas.models import Event

    target = Path(args.target)
    if not target.exists():
        print(f"Error: not found: {target}", file=sys.stderr)
        sys.exit(1)

    conversation: bool = args.conversation
    logger = make_logger(conversation=conversation)

    filter_types: set[str] = set(args.filter_types)
    skip_types: set[str] = set(args.skip_types)
    if conversation:
        filter_types = {"context_built", "llm_response_received"}

    since = _parse_date(args.since, "--since")
    until = _parse_date(args.until, "--until")

    files: list[Path]
    if target.is_file():
        files = [target]
    else:
        files = _collect_files(target, since=since, until=until)

    count = 0
    for f in files:
        for lineno, raw in _iter_lines(f):
            event_type = raw.get("type", "")
            if filter_types and event_type not in filter_types:
                continue
            if event_type in skip_types:
                continue
            try:
                event = Event.model_validate(raw)
            except Exception as e:
                print(f"[{f}:{lineno}] Event parse error: {e}", file=sys.stderr)
                continue
            logger(event)
            count += 1

    where = str(target) if target.is_file() else f"{target} ({len(files)} files)"
    print(f"\n({count} events replayed from {where})")


def _iter_lines(path: Path) -> Iterator[tuple[int, dict]]:
    try:
        with path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield lineno, json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"[{path}:{lineno}] JSON parse error: {e}",
                          file=sys.stderr)
                    continue
    except OSError as e:
        print(f"Error reading {path}: {e}", file=sys.stderr)


def _collect_files(
    root: Path, *, since: datetime | None, until: datetime | None,
) -> list[Path]:
    """Walk `root` recursively and return .jsonl files sorted by start-time.

    Filenames are `YYYY-MM-DDTHHMMSS[_<suffix>].jsonl`. Lexical sort across
    monthly subdirs preserves chronological order because the directory
    pattern `YYYY-MM/` also sorts lexically.
    """
    out: list[tuple[str, Path]] = []
    for p in root.rglob("*.jsonl"):
        date = _filename_start_date(p.name)
        if date is None:
            out.append((f"~{p.name}", p))
            continue
        if since is not None and date < since.date():
            continue
        if until is not None and date > until.date():
            continue
        # Combine month dir + filename so files in older months sort first.
        key = f"{p.parent.name}/{p.name}"
        out.append((key, p))
    out.sort(key=lambda x: x[0])
    return [p for _, p in out]


def _filename_start_date(name: str):
    m = _FILENAME_TS_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_date(s: str | None, flag: str):
    if s is None:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        print(f"Error: {flag} expects YYYY-MM-DD, got {s!r}", file=sys.stderr)
        sys.exit(1)


# ── purge ────────────────────────────────────────────────────────────────────


def run_purge(args: argparse.Namespace) -> None:
    if not args.before:
        print("Error: `reyn events purge` requires --before YYYY-MM-DD",
              file=sys.stderr)
        sys.exit(1)
    before = _parse_date(args.before, "--before")
    if before is None:
        sys.exit(1)

    root = Path(".reyn") / "events"
    if args.agent:
        root = root / "agents" / args.agent
    if not root.is_dir():
        print(f"No events directory at {root}", file=sys.stderr)
        return

    targets: list[Path] = []
    for p in root.rglob("*.jsonl"):
        date = _filename_start_date(p.name)
        if date is None:
            continue
        if date >= before.date():
            continue
        targets.append(p)

    if not targets:
        print(f"No event files older than {args.before}.")
        return

    targets.sort()
    for p in targets:
        print(("[dry-run] " if args.dry_run else "delete: ") + str(p))
    if args.dry_run:
        print(f"\n{len(targets)} files would be deleted.")
        return
    deleted = 0
    for p in targets:
        try:
            p.unlink()
            deleted += 1
        except OSError as e:
            print(f"  failed: {p}: {e}", file=sys.stderr)
    print(f"\nDeleted {deleted} of {len(targets)} files.")
