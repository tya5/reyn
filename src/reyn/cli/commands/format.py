"""`reyn format` — format DSL files into canonical form."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path


def register(sub) -> None:
    p = sub.add_parser("format", help="Format DSL files into canonical form")
    p.add_argument("--dsl", required=True, metavar="DIR",
                   help="Root directory of the DSL tree (e.g. dsl/)")
    p.add_argument("--check", action="store_true",
                   help="Dry-run: report files that would change without writing them")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from reyn.compiler.formatter import format_dsl

    dsl_root = Path(args.dsl)
    check_only = args.check
    changed = format_dsl(dsl_root, write=not check_only)

    if not changed:
        print("All files are already formatted.")
        return

    verb = "Would reformat" if check_only else "Reformatted"
    for p in changed:
        print(f"{verb}: {p}")

    if check_only:
        print(f"\n{len(changed)} file(s) would be reformatted.")
        sys.exit(1)
    else:
        print(f"\n{len(changed)} file(s) reformatted.")
