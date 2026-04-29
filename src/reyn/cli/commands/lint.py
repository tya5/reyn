"""`reyn lint` — lint a DSL app for issues."""
from __future__ import annotations
import argparse
import sys

from ..app_loader import resolve_app_path


def register(sub) -> None:
    p = sub.add_parser("lint", help="Lint a DSL app for issues")
    p.add_argument("app", metavar="APP",
                   help="App name to lint (same resolution as `reyn run <app>`)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from reyn.compiler.linter import lint_app_dir

    app_dir, _ = resolve_app_path(args.app)
    issues = lint_app_dir(app_dir)

    if not issues:
        print("No issues found.")
        return

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    for issue in issues:
        print(issue)

    print()
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")

    if errors:
        sys.exit(1)
