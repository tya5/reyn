#!/usr/bin/env python3
"""Assert a clean-break move left zero residual references to the old location.

Used after a clean-break package move (``reyn.chat`` -> ``reyn.runtime``) or a
symbol extraction (move a class out of a god-file into its own module). It
greps the repo for every way the old location can still be referenced and exits
non-zero if any survive — the "straggler 0" gate the C-series runs by hand.

Two modes:

    # symbol move: a class/func moved OUT of <old-module> into a new module.
    verify_package_move.py reyn.runtime.session RouterCapExceeded

    # package move: a whole module/package path was renamed/relocated.
    verify_package_move.py reyn.chat

Ref-classes checked:

  1. dotted-import   — ``from <old-module> import <symbol>`` (or any import of
     <old-module> for a package move). Parsed with ``ast``, so the
     single-line, multi-line, and parenthesised ``(\n  <symbol>,\n)`` forms are
     all caught by construction (the blind spot a line-based grep has).
  2. dotted-literal  — ``"<old-module>.<symbol>"`` / ``<old-module>.<symbol>``
     in source text (string refs, attribute access, docstrings, comments).
  3. segment-path    — the slash form ``<old/as/path>`` (package move only).
  4. bare-source-path— ``src/<old/as/path>`` (package move only).

stdlib-only (ast / re / argparse / pathlib) so CI can run it without deps,
mirroring scripts/test_tier_audit.py.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

DEFAULT_ROOTS = ("src", "tests")
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


def _py_files(roots: list[str]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        rp = Path(root)
        if not rp.exists():
            continue
        for p in rp.rglob("*.py"):
            if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in p.parts):
                continue
            out.append(p)
    return out


def _all_text_files(roots: list[str]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        rp = Path(root)
        if not rp.exists():
            continue
        for p in rp.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in p.parts):
                continue
            out.append(p)
    return out


def _import_hits(py_files: list[Path], old_module: str, symbol: str | None) -> list[str]:
    """Ref-class 1: imports of the old location, via AST (multi-line safe)."""
    hits: list[str] = []
    for p in py_files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module != old_module:
                    continue
                if symbol is None:
                    hits.append(f"{p}:{node.lineno}: from {node.module} import ...")
                else:
                    for alias in node.names:
                        if alias.name == symbol:
                            hits.append(
                                f"{p}:{node.lineno}: from {node.module} import {symbol}"
                            )
            elif isinstance(node, ast.Import) and symbol is None:
                for alias in node.names:
                    if alias.name == old_module or alias.name.startswith(old_module + "."):
                        hits.append(f"{p}:{node.lineno}: import {alias.name}")
    return hits


def _literal_hits(text_files: list[Path], needle: str, self_path: str | None) -> list[str]:
    """Ref-class 2: the dotted needle appearing literally in source text."""
    hits: list[str] = []
    pat = re.compile(re.escape(needle))
    for p in text_files:
        if self_path and str(p) == self_path:
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{p}:{i}: {line.strip()[:100]}")
        except UnicodeDecodeError:
            continue
    return hits


def _path_hits(text_files: list[Path], seg: str) -> list[str]:
    """Ref-classes 3+4: the slash form and the src/ prefixed slash form."""
    hits: list[str] = []
    pats = [re.compile(re.escape(seg)), re.compile(re.escape("src/" + seg))]
    for p in text_files:
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if any(pp.search(line) for pp in pats):
                    hits.append(f"{p}:{i}: {line.strip()[:100]}")
        except UnicodeDecodeError:
            continue
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("old_module", help="old dotted module path, e.g. reyn.runtime.session")
    ap.add_argument(
        "symbol",
        nargs="?",
        default=None,
        help="moved symbol name (symbol-move mode); omit for a package move",
    )
    ap.add_argument(
        "--roots",
        nargs="+",
        default=list(DEFAULT_ROOTS),
        help=f"dirs to scan (default: {' '.join(DEFAULT_ROOTS)})",
    )
    args = ap.parse_args(argv)

    py_files = _py_files(args.roots)
    text_files = _all_text_files(args.roots)
    self_path = "scripts/verify_package_move.py"

    sections: list[tuple[str, list[str]]] = []
    sections.append(("dotted-import (AST, multi-line safe)",
                     _import_hits(py_files, args.old_module, args.symbol)))

    if args.symbol is not None:
        sections.append((f"dotted-literal ({args.old_module}.{args.symbol})",
                         _literal_hits(text_files, f"{args.old_module}.{args.symbol}", self_path)))
    else:
        sections.append((f"dotted-literal ({args.old_module})",
                         _literal_hits(text_files, args.old_module, self_path)))
        seg = args.old_module.replace(".", "/")
        sections.append((f"segment-path / bare-source-path ({seg})",
                         _path_hits(text_files, seg)))

    total = sum(len(h) for _, h in sections)
    target = args.old_module + (f".{args.symbol}" if args.symbol else "")
    if total == 0:
        print(f"OK: 0 residual references to {target} (ref-classes clean).")
        return 0

    print(f"FAIL: {total} residual reference(s) to {target}:\n")
    for name, hits in sections:
        print(f"  [{name}] {len(hits)} hit(s)")
        for h in hits:
            print(f"    {h}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
