#!/usr/bin/env python3
"""#4008 — the bare-import sibling of the ``__file__``-depth class
(#3995/#4002/#4019, ``check_file_depth_reference.py``). A DIFFERENT
resolution mechanism, deliberately kept as its own gate rather than folded
into that one (see this issue's own body for the three reasons: distinct
AST shape, distinct population of zero at write time, and this arc's own
established convention of one gate per failure class).

## The failure this closes

``from _async_wait import wait_until`` — no ``tests.`` prefix, no relative
dots — resolves TODAY only because pytest's default "prepend" import mode
puts a FLAT consumer's own directory (``tests/``) onto ``sys.path``. A
consumer already living in a subdirectory (``tests/hooks/``) does NOT get
``tests/`` itself inserted (its own directory does, per the same import-mode
rule) — so the identical bare import silently ``ModuleNotFoundError``s the
moment that CONSUMER moves into a bucket, even though the imported module
(``tests/_async_wait.py``) itself never moved. 19 real instances of exactly
this were found and fixed the same night this issue was filed (converted to
``from tests._support.async_wait import wait_until`` — the same
depth-independent, ``REPO_ROOT``-anchored pattern the ``__file__`` class
converges on).

## Why this is a STATIC, add-time proxy, not a move-time check

Unlike ``check_migration_diff_shape.py``'s a′ (which needs a real move to
compare before/after), this failure is fully determined by a file's CURRENT
position: a nested file bare-importing a name that happens to match an
EXISTING flat ``tests/*.py`` basename is *already* relying on the "prepend"
mode's sys.path quirk today, whether or not that particular file ever moves
again. So the check is a pure population scan, no diff needed — mirroring
``check_file_depth_reference.py``'s own "whole-tree scan against a baseline
of zero" shape, verified fresh against the real tree (0 hits) before this
gate shipped, exactly as that gate's own docstring records doing.

## Scope

In scope: every ``tests/**/*.py`` file whose OWN directory is not
``tests_dir`` itself — a file already flat in ``tests/`` root cannot exhibit
this failure (its own directory already puts ``tests/`` on ``sys.path``, so
a bare import of a flat-module sibling name resolves correctly and stays
correct regardless of whether OTHER files move; if it moves off tests/
root, it stops being flat and enters this gate's own scope). ``conftest.py``
is excluded at any depth (see ``check_file_depth_reference.py``'s module
docstring — the one file class that structurally never moves).

Flagged: a top-level (``level == 0``) ``ast.ImportFrom`` whose ``module``'s
FIRST dotted component matches the basename (no ``.py``) of an existing
flat ``tests/*.py`` file. No name list, no hardcoded module names — a
structural comparison against the real, current flat population, so a
future flat file added tomorrow is covered automatically, the same way
``check_file_depth_reference.py``'s ``_support``/``fixtures`` detection
needs no hardcoded directory names either.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"
_STRUCTURALLY_EXEMPT = frozenset({"conftest.py"})


def flat_module_basenames(tests_dir: Path = _TESTS_DIR) -> "set[str]":
    """Every flat ``tests/*.py`` file's importable basename right now
    (``__init__.py`` excluded — a package marker, not an importable
    module name a bare import would ever name)."""
    return {
        p.stem for p in tests_dir.glob("*.py") if p.name != "__init__.py"
    }


def _bare_imports_of_a_flat_module(
    path: Path, flat_names: "set[str]"
) -> "list[str]":
    """The ``ImportFrom`` module names in *path* that are top-level
    (``level == 0``) and whose first dotted component names an existing
    flat ``tests/*.py`` module — the exact shape that resolves ONLY
    because of the consumer's own current (flat-adjacent) sys.path
    position, per the module docstring."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".", 1)[0]
            if top in flat_names:
                hits.append(node.module)
    return hits


def offending_files(tests_dir: Path = _TESTS_DIR) -> "list[tuple[Path, list[str]]]":
    """Every NESTED ``tests/**/*.py`` file (not flat in ``tests_dir`` itself)
    carrying at least one bare import of an existing flat module, paired
    with the offending module names — the gate's entire decision, isolated
    from CLI/printing so it is directly testable."""
    flat_names = flat_module_basenames(tests_dir)
    offenders: list[tuple[Path, list[str]]] = []
    for path in sorted(tests_dir.rglob("*.py")):
        if path.parent == tests_dir:
            continue  # a flat file's own directory already IS tests_dir
        if path.name in _STRUCTURALLY_EXEMPT:
            continue
        hits = _bare_imports_of_a_flat_module(path, flat_names)
        if hits:
            offenders.append((path, hits))
    return offenders


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-tree scan against a baseline of zero
    offenders = offending_files(_TESTS_DIR)

    if not offenders:
        print(
            "OK: no nested tests/ file bare-imports a name that matches "
            "an existing flat tests/*.py module."
        )
        return 0

    print("bare-tests-import-reference gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} file(s) bare-import a name that resolves ONLY "
        "because of their own current sys.path position (pytest's "
        "'prepend' import mode puts a FLAT consumer's directory on "
        "sys.path; a nested consumer's does not) — this SILENTLY BREAKS "
        "(ModuleNotFoundError, or a different-job skip) the moment the "
        "CONSUMER moves to a different bucket, even though the imported "
        "module itself never moved (#4008):",
        file=sys.stderr,
    )
    for path, modules in offenders:
        rel = path.relative_to(_ROOT)
        for module in modules:
            print(f"  {rel}: from {module} import ...", file=sys.stderr)

    print(
        "\nUse an explicit, depth-independent import instead — e.g. "
        "`from tests._support.<name> import ...` (requires tests/__init__.py, "
        "already present, #4001) rather than a bare top-level name.\n"
        "\nThis gate's own starting population is zero, so any hit here is "
        "a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
