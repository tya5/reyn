#!/usr/bin/env python3
"""Suggest a subdirectory for a new test file — advisory only, never enforced.

#3879 Stage 0 (architect's design, issue #3879 comments 5228613324/5228618772):
the placement rule is "the ``reyn.<second-level-package>`` this file imports
most" — a mirror of ``src/reyn/``, so "where does this subsystem's tests live"
and "what does this subsystem import" are the same question and never drift
apart (no second source of truth to keep in sync, unlike an invented axis such
as Tier or a hand-picked functional area — see the issue for the measurement
that ruled those out).

``schemas`` / ``config`` / ``data`` are excluded from the count as
foundational — nearly every test imports one of them regardless of its real
subject, so counting them dilutes the signal rather than sharpening it. A file
whose reyn imports are ONLY from the excluded set (or has none at all) gets no
suggestion — printed plainly, not guessed.

Deliberately advisory, not enforced: measured 36% of files (388/1,067) flip
their dominant package on a single import line added or removed, 17% of those
by pure alphabetical tie-break — forcing CI to require a specific directory
would make an ordinary refactor's import-line diff fail the gate for a reason
unrelated to the refactor itself. See ``check_no_new_flat_tests.py`` for the
one thing Stage 0 DOES enforce (no new file lands directly in ``tests/``).
"""
from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from pathlib import Path

# Foundational packages every test imports regardless of subject — excluded
# from the dominant-package count so they never win it (see module docstring).
_FOUNDATIONAL = frozenset({"schemas", "config", "data"})


def dominant_package(source: str) -> str | None:
    """Return the ``reyn.<pkg>`` this source imports most often (excluding
    :data:`_FOUNDATIONAL`), tie-broken alphabetically. ``None`` when no
    non-foundational ``reyn.*`` import is present."""
    tree = ast.parse(source)
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # A relative import (node.level > 0) has no dotted `reyn...` module
            # name to read here — irrelevant for a top-level tests/ file, and
            # skipped rather than mis-parsed as a bare "reyn" import.
            if node.level == 0:
                modules.append(node.module)
        for mod in modules:
            parts = mod.split(".")
            if len(parts) >= 2 and parts[0] == "reyn":
                pkg = parts[1]
                if pkg not in _FOUNDATIONAL:
                    counts[pkg] += 1
    if not counts:
        return None
    best = max(counts.values())
    # Tie-break alphabetically — deterministic regardless of who runs it.
    return min(pkg for pkg, n in counts.items() if n == best)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Suggest a tests/ subdirectory for a new test file (#3879 Stage 0). "
            "Advisory only — prints a suggestion, changes nothing, enforces nothing."
        ),
    )
    parser.add_argument("path", help="Path to the test file to suggest a directory for.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.path)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 1

    pkg = dominant_package(source)
    if pkg is None:
        print(
            f"{path}: no non-foundational `reyn.*` import found — "
            "no suggestion (schemas/config/data alone don't count)."
        )
        return 0

    print(f"{path}: suggested tests/{pkg}/ (dominant import: reyn.{pkg})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
