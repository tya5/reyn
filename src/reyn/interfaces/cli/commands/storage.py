"""`reyn storage stats` — read-only on-disk footprint report for
``.reyn/media/``, ``.reyn/memory/history-content/`` (#4478 Phase 1, #5364
moved the write location), and every ``history.jsonl`` under
``.reyn/agents/`` (#4476 Phase 1).

`reyn storage migrate-manifest` — #5896 stage ② item ①: the one-time,
idempotent backfill that gives every already-written history-content file
a real spill-manifest line (see
``reyn.data.workspace.media_store.migrate_history_content_manifest``'s own
docstring for the mechanism). Unlike ``stats`` this one WRITES (a manifest
line, never a history-content body) — the only mutation this module made
until stage ③ below.

`reyn storage migrate-bodies` — #5896 stage ③ (owner-hit P0): moves an
already-written ``history.jsonl`` row's INLINE tool-result body out to a
``history-content/`` file, for rows written BEFORE stage ① started doing
this at return time. See
``reyn.runtime.services.history_body_migration.migrate_inline_history_bodies``'s
own docstring for the write-ahead / atomicity / ``.bak`` / interrupted-run
contract — this is the one command in this module that can write a NEW
history-content body (``migrate-manifest`` only ever writes a manifest
line for a body that already exists). **Run with the target session
stopped** — this rewrites ``history.jsonl`` on disk; a live session's own
in-memory ``self.history`` would not see the change and a concurrent
write from that session could race this command's own read.

Also prints THIS process's own ``phys_footprint``/``rss`` at start and
end (architect ruling, follow-up to PR #5947 — #5851's own reader,
``reyn.runtime.process_memory``): pairs this run's own cost against the
effect the owner's own acceptance criterion measures separately (a
post-migration STARTUP peak an order of magnitude smaller) — both
numbers from the SAME run, not two an operator has to correlate by
hand. Two snapshots only, never a duration.

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
from typing import Callable


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

    migrate_bodies_p = storage_sub.add_parser(
        "migrate-bodies",
        help=(
            "#5896 stage ③: move an already-written history.jsonl row's "
            "INLINE tool-result body out to a history-content/ file — "
            "for rows predating stage ①'s return-time write. Run with "
            "the target session stopped."
        ),
    )
    migrate_bodies_p.add_argument(
        "--project-root",
        default=".",
        help="Project root containing .reyn/ (default: current directory).",
    )
    migrate_bodies_p.add_argument(
        "--agent",
        default=None,
        help=(
            "Migrate only this agent's history.jsonl (default: every "
            "agent under .reyn/agents/)."
        ),
    )
    migrate_bodies_p.add_argument(
        "--min-bytes",
        type=int,
        default=None,
        help=(
            "Only migrate a row whose inline body is at least this many "
            "bytes (default: history_body_migration.DEFAULT_MIN_BYTES, "
            "1 MiB — see that module's own docstring for the rationale)."
        ),
    )
    migrate_bodies_p.set_defaults(func=run_migrate_bodies)


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


def _print_footprint(label: str, reader: "Callable[[], int | None]", metric: "str | None") -> None:
    value = reader()
    if metric is not None and value is not None:
        print(f"footprint ({label}): {value:,} bytes ({metric})")
    else:
        print(f"footprint ({label}): not measurable on this platform")


def run_migrate_bodies(args: argparse.Namespace) -> None:
    """#5896 stage ③ (owner-hit P0) — see
    ``history_body_migration.migrate_inline_history_bodies``'s own
    docstring for the write-ahead/atomicity/``.bak``/interrupted-run
    contract. One ``MediaStore`` per agent (write destination is
    per-agent, per ``history_content_root_for``'s own path shape) —
    ``session_id="storage-migrate"`` is a fixed, clearly-labeled value
    for this offline tool, not tied to any live session (nothing else
    ever needs to guess it back; the row's own ``content_ref`` is the
    only thing a future reader follows).

    Also reports THIS process's own footprint at start and end (#5851's
    own reader, ``reyn.runtime.process_memory`` — architect ruling,
    follow-up to PR #5947): pairs this run's own COST (it streams one
    row at a time — see ``migrate_inline_history_bodies``'s own
    docstring for why a single oversized row is still a real, bounded
    peak — but writing N migrated bodies and rewriting the file is real
    work) against the EFFECT the owner's own acceptance criterion
    measures separately (a post-migration startup peak an order of
    magnitude smaller) — both numbers from the SAME run, not two
    separate ones an operator has to correlate by hand. Two points only
    (start, end) — never a byte figure derived from elapsed time, this
    is a snapshot pair, not a duration."""
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
    from reyn.runtime.process_memory import (
        make_process_memory_reader,
        process_memory_metric_name,
    )
    from reyn.runtime.services.history_body_migration import (
        DEFAULT_MIN_BYTES,
        migrate_inline_history_bodies,
    )

    metric = process_memory_metric_name()
    reader = make_process_memory_reader()
    _print_footprint("start", reader, metric)

    project_root = Path(args.project_root).resolve()
    min_bytes = args.min_bytes if args.min_bytes is not None else DEFAULT_MIN_BYTES
    agents_dir = project_root / ".reyn" / "agents"
    if not agents_dir.is_dir():
        print("no .reyn/agents/ directory — nothing to migrate.")
        _print_footprint("end", reader, metric)
        return

    if args.agent is not None:
        history_paths = [agents_dir / args.agent / "history.jsonl"]
    else:
        history_paths = sorted(agents_dir.glob("*/history.jsonl"))

    total_migrated = 0
    total_reused = 0
    total_written = 0
    for hist_path in history_paths:
        if not hist_path.is_file():
            continue
        agent_name = hist_path.parent.name
        store = MediaStore(
            MediaStoreConfig(),
            project_root=project_root,
            agent_name=agent_name,
            session_id="storage-migrate",
        )

        def _save(content: str, *, _store: MediaStore = store) -> str:
            result = _store.save_tool_result(content, spilled=False, tool="storage-migrate")
            return result["path"]

        try:
            result = migrate_inline_history_bodies(
                hist_path, save_fn=_save, min_bytes=min_bytes,
            )
        except FileExistsError as exc:
            print(f"{agent_name}: SKIPPED — {exc}")
            continue
        total_migrated += result["migrated"]
        total_reused += result["reused_ref"]
        total_written += result["bytes_written"]
        if result["migrated"]:
            print(
                f"{agent_name}: migrated {result['migrated']} row(s) "
                f"({result['reused_ref']} reused an existing ref, "
                f"{result['bytes_written']:,} new byte(s) written; "
                f"backup at {hist_path.name}.bak)",
            )
        else:
            print(f"{agent_name}: nothing over {min_bytes:,} bytes to migrate.")

    _print_footprint("end", reader, metric)
    print(
        f"\ntotal: {total_migrated} row(s) migrated, {total_reused} reused "
        f"an existing ref, {total_written:,} new byte(s) written.",
    )
