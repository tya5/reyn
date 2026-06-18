#!/usr/bin/env python3
"""Fail if a MODULE docstring carries moved-module refactor narrative.

The C-series discipline (and #311 / #1787 / #1790 before it): a module's
docstring describes the **current state** — what the module IS — and the
**refactor story** (extracted-from / proposal id / staging) lives in the commit
message, not the docstring. This catches the most common violation shape
mechanically so it isn't re-flagged by hand each extraction.

Scope is deliberately narrow to keep **zero false positives**: only MODULE
docstrings (extracted via ``ast`` — class/func docstrings are never inspected,
so a class's legitimate ``#383`` contract ref is out of scope), and only the
precise anti-pattern shape — an **extract/move/split verb** pointing at a
**source module** (a ``reyn.`` dotted path, a ``*.py`` filename, or the words
"god-file" / "decomposition"). Bare ``#NNNN`` / ``FP-NNNN`` refs are NOT
flagged: they pervade legitimate current-state design-context docstrings (a
calibration scan found 94 / 121 such module docstrings on main), so flagging
them would make the gate cry wolf. The trade-off is accepted false negatives
on creative phrasings — those stay covered by review.

stdlib-only (ast / re / argparse / pathlib), mirroring
scripts/test_tier_audit.py, so CI runs it dep-free.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}

# An extract/move verb whose object is a *source module* (not a concept).
# Kept tight so legitimate prose ("extract the token count", "moved the cursor")
# doesn't trip — the object must look like a module/file or the decomposition
# vocabulary.
_VERB = r"(?:extract(?:ed|s|ing)?|moved|split(?:\s+out)?|relocat(?:ed|es|ing)?|pulled)"
_SOURCE = (
    r"(?:"
    r"reyn\.\w[\w.]*"          # a reyn dotted module path
    r"|\b\w+\.py\b"            # a *.py filename
    r"|god-file"              # the decomposition vocabulary
    r"|decompos\w*"
    r")"
)
NARRATIVE = re.compile(
    rf"(?is)\b{_VERB}\b[^.\n]{{0,60}}?\b(?:from|out of)\b[^.\n]{{0,60}}?{_SOURCE}"
)


def _py_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".py":
            out.append(p)
        elif p.is_dir():
            for f in p.rglob("*.py"):
                if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in f.parts):
                    continue
                out.append(f)
    return out


def violations(paths: list[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for p in _py_files(paths):
        try:
            doc = ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")), clean=False)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if not doc:
            continue
        m = NARRATIVE.search(doc)
        if m:
            found.append((str(p), " ".join(m.group(0).split())[:90]))
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "paths",
        nargs="*",
        default=["src/reyn"],
        help="files or dirs to scan (default: src/reyn)",
    )
    args = ap.parse_args(argv)
    paths = args.paths or ["src/reyn"]

    found = violations(paths)
    if not found:
        print(f"OK: no module-docstring refactor narrative in {' '.join(paths)}.")
        return 0

    print(f"FAIL: {len(found)} module docstring(s) carry refactor narrative.")
    print("Move the refactor story (extracted-from / proposal id / staging) to "
          "the commit message; keep the module docstring current-state only.\n")
    for fp, snippet in found:
        print(f"  {fp}\n      :: {snippet!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
