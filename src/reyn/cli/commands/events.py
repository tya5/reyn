"""`reyn events` — replay a saved event JSONL file."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from ..logger_factory import make_logger


def register(sub) -> None:
    p = sub.add_parser("events", help="Replay a saved event JSONL file to the console")
    p.add_argument(
        "path", metavar="FILE",
        help="Path to the .jsonl event file (e.g. workspace/runs/20260426T…_app_builder.jsonl)",
    )
    p.add_argument("--rich", action="store_true",
                   help="Use Rich-styled output instead of plain text")
    p.add_argument(
        "--filter", metavar="TYPE", action="append", dest="filter_types", default=[],
        help="Only show events of this type (repeatable, e.g. --filter phase_started --filter phase_completed)",
    )
    p.add_argument(
        "--skip", metavar="TYPE", action="append", dest="skip_types", default=[],
        help="Skip events of this type (repeatable)",
    )
    p.add_argument(
        "--conversation", action="store_true",
        help=(
            "Show LLM conversation history: display context frames sent to the LLM "
            "and the raw responses received, in order. "
            "Overrides --filter and --skip."
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from reyn.models import Event

    path = Path(args.path)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    conversation: bool = args.conversation
    logger = make_logger(rich=args.rich, conversation=conversation)

    filter_types: set[str] = set(args.filter_types)
    skip_types: set[str] = set(args.skip_types)
    if conversation:
        filter_types = {"context_built", "llm_response_received"}

    count = 0
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[line {lineno}] JSON parse error: {e}", file=sys.stderr)
                continue

            event_type = raw.get("type", "")
            if filter_types and event_type not in filter_types:
                continue
            if event_type in skip_types:
                continue

            try:
                event = Event.model_validate(raw)
            except Exception as e:
                print(f"[line {lineno}] Event parse error: {e}", file=sys.stderr)
                continue

            logger(event)
            count += 1

    print(f"\n({count} events replayed from {path})")
