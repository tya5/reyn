"""`reyn media stats` — read-only on-disk footprint report for
``.reyn/media/``, ``.reyn/tool-results/`` (#4478 Phase 1), and every
``history.jsonl`` under ``.reyn/agents/`` (#4476 Phase 1).

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
        "media", help="Inspect Reyn-managed media / tool-result / history storage",
    )
    media_sub = p.add_subparsers(dest="media_command", metavar="<subcommand>")
    media_sub.required = True
    stats_p = media_sub.add_parser(
        "stats",
        help=(
            "Print on-disk file counts + byte totals for "
            ".reyn/media/, .reyn/tool-results/, and every history.jsonl"
        ),
    )
    stats_p.add_argument(
        "--project-root",
        default=".",
        help="Project root containing .reyn/ (default: current directory).",
    )
    stats_p.set_defaults(func=run_stats)


def run_stats(args: argparse.Namespace) -> None:
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
    from reyn.runtime.history_tail_reader import aggregate_history_stats

    project_root = Path(args.project_root).resolve()
    store = MediaStore(MediaStoreConfig(), project_root=project_root)
    stats = store.storage_stats()
    hist = aggregate_history_stats(project_root)

    print(f"{'directory':<16}{'files':>10}{'bytes':>16}")
    print(f"{'media/':<16}{stats.media_file_count:>10}{stats.media_bytes:>16,}")
    print(
        f"{'tool-results/':<16}"
        f"{stats.tool_result_file_count:>10}{stats.tool_result_bytes:>16,}",
    )
    print()
    print(f"{'':<16}{'files':>10}{'bytes':>16}{'turns':>12}")
    print(
        f"{'history.jsonl':<16}"
        f"{hist.file_count:>10}{hist.total_bytes:>16,}{hist.total_lines:>12,}",
    )
