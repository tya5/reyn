"""`reyn lint` — lint a DSL app for issues."""
from __future__ import annotations

import argparse
import sys

from ..skill_loader import resolve_skill_path


def register(sub) -> None:
    p = sub.add_parser("lint", help="Lint a DSL skill for issues")
    p.add_argument("skill", metavar="SKILL",
                   help="Skill name to lint (same resolution as `reyn run <skill>`)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from reyn.core.compiler.linter import lint_skill_dir

    app_dir, _ = resolve_skill_path(args.skill)
    issues = lint_skill_dir(app_dir)

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
