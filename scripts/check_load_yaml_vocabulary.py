#!/usr/bin/env python3
"""#5455 ②: a static gate — every call to
``reyn.config.loader._load_yaml`` passes a ``vocabulary=`` keyword
argument.

## The class this closes

``_load_yaml(path, *, vocabulary)`` has no default for ``vocabulary`` —
omitting it is a ``TypeError`` at the call site, so it already cannot be
imported into a NEW file that forgets to decide. But ``_load_yaml`` is a
module-private helper: nothing stops a future refactor from giving it a
default again "for convenience", silently reopening the exact #4501/#4515
hole this issue exists to close (a new operator-editable yaml file ships
with no unknown-key disclosure, and nothing says so). This gate pins the
CALL-SITE shape directly — a `vocabulary=` keyword argument present at
every `_load_yaml(` call — so a regression on the SIGNATURE side (the
default quietly coming back) still shows up here even though it would no
longer be a Python-level TypeError.

## Why AST, not a signature-default check

Checking "does `_load_yaml` still lack a default" answers only half the
question — it says nothing about whether some caller has silently reached
in via `_load_yaml(path)` after a hypothetical future refactor. Statically
finding every call site and requiring the keyword there is the same
"witness the actual failure mode, not a stand-in for it" discipline the
`vocabulary` parameter's own docstring names as this issue's driving
principle: the failure is a NEW CALL SITE deciding nothing, not a NEW
DEFAULT VALUE existing.

## Real source is the reader

This gate parses the real `src/` tree and reports the real line/file of
any offending call — nothing here is a fixture; a strip (deleting a
`vocabulary=` kwarg from any current call site) turns this RED against
the actual codebase.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"


class _CallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: "list[tuple[int, str]]" = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast API name)
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if name == "_load_yaml":
            has_vocabulary = any(kw.arg == "vocabulary" for kw in node.keywords)
            if not has_vocabulary:
                self.violations.append((node.lineno, ast.dump(node)[:120]))
        self.generic_visit(node)


def find_violations(src_dir: Path = _SRC) -> "list[tuple[Path, int]]":
    """Every ``_load_yaml(...)`` call under *src_dir* missing a
    ``vocabulary=`` keyword argument — the definition site itself
    (``def _load_yaml``) is not a call and is never matched. *src_dir*
    defaults to this repo's real ``src/`` tree; a test overrides it with
    a synthetic ``tmp_path`` tree."""
    violations: "list[tuple[Path, int]]" = []
    for path in sorted(src_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        visitor = _CallVisitor()
        visitor.visit(tree)
        for lineno, _ in visitor.violations:
            violations.append((path, lineno))
    return violations


def main() -> int:
    violations = find_violations()
    if not violations:
        print("OK: every _load_yaml(...) call passes vocabulary= explicitly.")
        return 0
    print("load-yaml-vocabulary gate FAILED:\n")
    print(
        f"{len(violations)} call(s) to _load_yaml(...) omit the required "
        f"vocabulary= keyword argument:\n"
    )
    for path, lineno in violations:
        print(f"  {path.relative_to(_REPO_ROOT)}:{lineno}")
    print(
        "\nPass vocabulary=<unknown_config_keys-shaped callable> to WARN on "
        "this file's own unknown keys at read time, or vocabulary=None — "
        "EXPLICITLY, with a comment naming where the check actually "
        "happens — if this file's content is validated elsewhere. See "
        "_load_yaml's own docstring (src/reyn/config/loader.py)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
