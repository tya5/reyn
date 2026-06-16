"""Tier 2: #1682 #3 — the config/ package re-exports the full de-facto-public surface.

The god-module ``config.py`` was split into a ``config/`` package. ~135 files do
``from reyn.config import X`` — including de-facto-public underscore privates
(``_find_project_root`` has 23 importers, many ``_build_*``, module constants like
``_DEFAULT_EMBEDDING_CLASSES``). If the package ``__init__`` omits ANY imported
name, those call sites break (R4). This guard derives the union of every name
imported from ``reyn.config`` across src + tests MECHANICALLY and asserts each
resolves on ``reyn.config`` — so an omission fails here, not in 50 unrelated tests.
"""
from __future__ import annotations

import ast
from pathlib import Path

import reyn.config


def _imported_names() -> set[str]:
    """Every name imported via ``from reyn.config import …`` (exact module, not a
    submodule) across src + tests + scripts. Uses ast so docstring/comment prose
    that merely mentions the phrase is never matched."""
    root = Path(__file__).resolve().parent.parent  # repo root (has src/ + tests/)
    names: set[str] = set()
    for sub in ("src", "tests", "scripts"):
        base = root / sub
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "reyn.config":
                    for alias in node.names:
                        if alias.name != "*":
                            names.add(alias.name)
    return names


def test_every_imported_config_name_is_reexported() -> None:
    """Tier 2: #1682 #3 (R4 guard) — every name imported from reyn.config across the
    codebase resolves on the package. Catches a re-export omission directly."""
    names = _imported_names()
    # sanity: the scan found a substantial surface (not a broken/empty regex).
    assert "ReynConfig" in names and "load_config" in names and "_find_project_root" in names
    missing = sorted(n for n in names if not hasattr(reyn.config, n))
    assert not missing, (
        f"reyn.config does not re-export {len(missing)} imported name(s): {missing} "
        "— add them to the config/ package __init__ re-export."
    )
