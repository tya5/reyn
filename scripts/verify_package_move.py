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
    verify_package_move.py reyn.plugins
    verify_package_move.py reyn.plugins --new reyn.gateway   # explicit + clearer

The positional ``symbol`` is for symbol moves only. For a PACKAGE rename pass
the old path alone (or, clearer, ``--new <new-pkg>``); passing the new package
as a second positional would read it as a *symbol*, so a dotted second
positional is rejected with a hint to use ``--new``.

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
# Repo-root config that carries package references outside src/tests — entry
# points (dotted literals) + package-data / find paths. A package move that
# repoints only src/tests/docs misses these (the #1807 webhooks entry-point
# regression), so they are always scanned for the dotted-literal / path checks.
ROOT_CONFIG_FILES = ("pyproject.toml", "setup.cfg", "setup.py", "MANIFEST.in")


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
        "--new",
        default=None,
        metavar="NEW_PKG",
        help="new package path for a package rename (e.g. reyn.gateway). Makes "
             "package-move mode explicit; the positional symbol is ignored. The "
             "check asserts the OLD path has 0 residual references.",
    )
    ap.add_argument(
        "--roots",
        nargs="+",
        default=list(DEFAULT_ROOTS),
        help=f"dirs to scan (default: {' '.join(DEFAULT_ROOTS)})",
    )
    args = ap.parse_args(argv)

    # Guard the common trap: `verify_package_move.py <old> <new>` where the 2nd
    # positional is actually a package path (dotted) — it would be read as a
    # symbol. For a package rename, use --new.
    if args.new is None and args.symbol is not None and "." in args.symbol:
        ap.error(
            f"symbol '{args.symbol}' looks like a dotted module path. "
            f"For a package rename use: --new {args.symbol}"
        )
    # --new makes package-move mode explicit (the positional symbol is ignored).
    symbol = None if args.new is not None else args.symbol

    py_files = _py_files(args.roots)
    text_files = _all_text_files(args.roots)
    # Always include repo-root config (entry points / package-data live here,
    # outside the src/tests roots) for the dotted-literal + path-hit checks.
    text_files += [Path(f) for f in ROOT_CONFIG_FILES if Path(f).is_file()]
    self_path = "scripts/verify_package_move.py"

    sections: list[tuple[str, list[str]]] = []
    sections.append(("dotted-import (AST, multi-line safe)",
                     _import_hits(py_files, args.old_module, symbol)))

    if symbol is not None:
        sections.append((f"dotted-literal ({args.old_module}.{symbol})",
                         _literal_hits(text_files, f"{args.old_module}.{symbol}", self_path)))
    else:
        sections.append((f"dotted-literal ({args.old_module})",
                         _literal_hits(text_files, args.old_module, self_path)))
        seg = args.old_module.replace(".", "/")
        sections.append((f"segment-path / bare-source-path ({seg})",
                         _path_hits(text_files, seg)))

    total = sum(len(h) for _, h in sections)
    target = args.old_module + (f".{symbol}" if symbol else "")
    if args.new is not None:
        target = f"{args.old_module} (renamed → {args.new})"
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
