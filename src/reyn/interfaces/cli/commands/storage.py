"""`reyn storage stats` — read-only on-disk footprint report for
``.reyn/media/``, ``.reyn/memory/history-content/`` (#4478 Phase 1, #5364
moved the write location), and every ``history.jsonl`` under
``.reyn/agents/`` (#4476 Phase 1).

`reyn storage migrate-manifest` — #5896 stage ② item ①: the one-time,
idempotent backfill that gives every already-written history-content file
a real spill-manifest line (see
``reyn.data.workspace.media_store.migrate_history_content_manifest``'s own
docstring for the mechanism). Unlike ``stats`` this one WRITES (a manifest
line, never a history-content body) — the only mutation this module makes.

Named ``storage``, not ``media`` (renamed from the original #4485 name once
#4476 landed on the same command — lead-coder review on #4488): once
``history.jsonl`` reports through here too, "media" no longer describes
what the command covers. ``storage`` is the name all three share — "how
much of reyn's own on-disk footprint currently exists" — and stays correct
as more subsystems land measurement here.

Exists so these measurement methods — each named by their own subsystem's
module docstring as the precondition for a future Phase 2 eviction/retention
policy ("trigger is measurement evidence, not hypothesis") — have an actual
caller. A measurement method with no reader is the shape this repo has hit
repeatedly: declared, implemented, tested, invoked by nobody. This command
is that reader: an operator (or a script) runs it to decide whether disk
pressure is real BEFORE any TTL/max-N/retention policy gets designed. No
deletion, no policy, no threshold — see `media_store.py` and
`history_tail_reader.py` for why those stay out of scope here.

#4476 lands on this SAME surface rather than a second command (lead-coder
review: "揃える" — align to what #4485 already built) — both are the same
shape (policy-independent bytes/count snapshot feeding an eventual owner
retention decision), so one operator-facing place to look is more honest
than two commands that answer the same underlying question for different
subsystems.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def register(sub) -> None:
    p = sub.add_parser(
        "storage", help="Inspect Reyn-managed media / tool-result / history storage",
    )
    storage_sub = p.add_subparsers(dest="storage_command", metavar="<subcommand>")
    storage_sub.required = True
    stats_p = storage_sub.add_parser(
        "stats",
        help=(
            "Print on-disk file counts + byte totals for "
            ".reyn/media/, .reyn/memory/history-content/, and every history.jsonl"
        ),
    )
    stats_p.add_argument(
        "--project-root",
        default=".",
        help="Project root containing .reyn/ (default: current directory).",
    )
    stats_p.set_defaults(func=run_stats)

    migrate_p = storage_sub.add_parser(
        "migrate-manifest",
        help=(
            "#5896 stage ② item ①: give every history-content file with no "
            "spill-manifest line a real one (one-time, idempotent — a "
            "second run migrates nothing)."
        ),
    )
    migrate_p.add_argument(
        "--project-root",
        default=".",
        help="Project root containing .reyn/ (default: current directory).",
    )
    migrate_p.set_defaults(func=run_migrate_manifest)


def run_stats(args: argparse.Namespace) -> None:
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
    from reyn.runtime.history_tail_reader import aggregate_history_stats

    project_root = Path(args.project_root).resolve()
    # #5364: read-only here (storage_stats never writes) — session_id is
    # a required kwarg (no default: a forgotten value must never silently
    # resolve to a real session's directory, #5369) but this store never
    # calls save_tool_result, so the value itself is inert.
    store = MediaStore(
        MediaStoreConfig(), project_root=project_root, session_id="<read-only>",
    )
    stats = store.storage_stats()
    hist = aggregate_history_stats(project_root)

    print(f"{'directory':<26}{'files':>10}{'bytes':>16}")
    print(f"{'media/':<26}{stats.media_file_count:>10}{stats.media_bytes:>16,}")
    # #5364: tool-result writes now live under memory/history-content/
    # (nested per session, GB-class), not tool-results/ — the label
    # reflects where the bytes actually are.
    print(
        f"{'memory/history-content/':<26}"
        f"{stats.tool_result_file_count:>10}{stats.tool_result_bytes:>16,}",
    )
    print()
    print(f"{'':<16}{'files':>10}{'bytes':>16}{'turns':>12}")
    print(
        f"{'history.jsonl':<16}"
        f"{hist.file_count:>10}{hist.total_bytes:>16,}{hist.total_lines:>12,}",
    )


def run_migrate_manifest(args: argparse.Namespace) -> None:
    """#5896 stage ② item ①: the one-time, idempotent operator command —
    see ``migrate_history_content_manifest``'s own docstring for the
    mechanism (unknown-truth files default un-spilled, "不明なら守る")."""
    from reyn.data.workspace.media_store import migrate_history_content_manifest
    from reyn.runtime.services.router_history_buffer import iter_history_content_refs

    project_root = Path(args.project_root).resolve()
    result = migrate_history_content_manifest(
        project_root, iter_content_refs=iter_history_content_refs,
    )
    print(
        f"migrated {result['migrated']} file(s) into the spill manifest "
        f"({result['protected_unknown']} defaulted un-spilled — no "
        "history.jsonl row named them, so 'unknown ⇒ protect' applies).",
    )
