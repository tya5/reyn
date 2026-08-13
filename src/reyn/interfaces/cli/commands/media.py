"""`reyn media stats` — read-only on-disk footprint report for
``.reyn/media/`` and ``.reyn/tool-results/`` (#4478 Phase 1).

Exists so ``MediaStore.storage_stats`` — the measurement `media_store.py`'s
own module docstring names as the precondition for any future Phase 2
eviction policy ("trigger is measurement evidence, not hypothesis") — has an
actual caller. A measurement method with no reader is the shape this repo
has hit repeatedly: declared, implemented, tested, invoked by nobody. This
command is that reader: an operator (or a script) runs it to decide whether
disk pressure is real BEFORE any TTL/max-N policy gets designed. No
deletion, no policy, no threshold — see `media_store.py` for why those stay
out of scope here.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def register(sub) -> None:
    p = sub.add_parser(
        "media", help="Inspect Reyn-managed media / tool-result storage",
    )
    media_sub = p.add_subparsers(dest="media_command", metavar="<subcommand>")
    media_sub.required = True
    stats_p = media_sub.add_parser(
        "stats",
        help=(
            "Print on-disk file counts + byte totals for "
            ".reyn/media/ and .reyn/tool-results/"
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

    project_root = Path(args.project_root).resolve()
    store = MediaStore(MediaStoreConfig(), project_root=project_root)
    stats = store.storage_stats()
    print(f"{'directory':<16}{'files':>10}{'bytes':>16}")
    print(f"{'media/':<16}{stats.media_file_count:>10}{stats.media_bytes:>16,}")
    print(
        f"{'tool-results/':<16}"
        f"{stats.tool_result_file_count:>10}{stats.tool_result_bytes:>16,}",
    )
