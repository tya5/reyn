#!/usr/bin/env python3
"""#6143 -- a `src/reyn/**` population gate over `warnings.warn(..., category)`
call sites whose category Python IGNORES by default outside `__main__`.

## Why this exists

#6143's own measurement:

    $ python3 -c "import warnings; [print(f) for f in warnings.filters[:5]]"
    ('default', None, DeprecationWarning, '__main__', 0)
    ('ignore',  None, DeprecationWarning, None, 0)          <- not __main__
    ('ignore',  None, PendingDeprecationWarning, None, 0)
    ('ignore',  None, ImportWarning, None, 0)
    ('ignore',  None, ResourceWarning, None, 0)

Every module under `src/reyn/` is NOT `__main__`, so a `warnings.warn(...,
DeprecationWarning)` (or PendingDeprecationWarning / ImportWarning /
ResourceWarning) fired from `src/reyn/**` reaches nobody in a real run --
`logging.captureWarnings(True)` only redirects `showwarning`, it does not
change the filter that decides whether `showwarning` is ever called at
all, and `pyproject.toml`'s `[tool.pytest.ini_options] filterwarnings` is
pytest-only. The same shape was hit 3 times in one night (#6132, #6137,
and #6141 -- the last one nearly landed with lead-coder's own requested
fix silently swallowed, caught only by co-vet measuring a real process).
AST-derived census on `origin/main` before this issue's own fix: 7 sites
(`config/chat.py` x5, `security/permissions/permissions.py` x2) -- all 7
were promoted to `logger.warning` in the same PR that added this gate.

## What this gate checks

For every `warnings.warn(...)` call under `src/reyn/**`, if the category
argument (positional arg 2, or a `category=` keyword) is a bare name (or
attribute) matching one of the silent-by-default set below, the call site
must appear in this script's own `_EXCEPTION_TABLE` with a reason -- or
the gate is RED. There is no other escape hatch (no comment-based
suppression): a genuinely developer-only warning stays reviewed, in one
place, not decided ad hoc at each call site.

This is a TABLE, not a ratchet (contrast `silent_except_ratchet.py`,
#5990): the measured population is 0 as of this gate landing (every known
site was fixed in the same PR), so there is nothing to grandfather. A
future PR that adds a new silent-by-default `warnings.warn` either
promotes it to `logger.warning` (or a stronger surface, e.g. a
`project_status`/Ctx-pane field -- see #6139) or adds a table entry with
a one-line reason, in the SAME PR.

## Deliberately narrow category resolution (disclosed, not solved)

Category resolution only recognises a literal `ast.Name` or the `.attr`
of an `ast.Attribute` (e.g. `warnings.DeprecationWarning` -- unusual but
legal) -- it does not evaluate expressions, resolve imports, or follow a
variable holding a category class. A call site that passes a silent
category through an indirection this script cannot see is a FALSE
REJECT never flagged -- the same "suspected, not confirmed" posture
`suspected_time_dependence_ratchet.py` (#4846) and `silent_except_
ratchet.py` (#5990) both already carry for a syntax-only AST gate.

CI: gate
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCOPE = "src/reyn"

# Silent-by-default OUTSIDE __main__ -- see module docstring's own
# `warnings.filters` measurement. NOT UserWarning (Python's own default
# category when none is given) -- that one IS shown (once per call site)
# under the stock filter, so a bare `warnings.warn("...")` or an explicit
# `UserWarning` is out of this gate's population on purpose.
_SILENT_BY_DEFAULT_CATEGORIES = frozenset({
    "DeprecationWarning", "PendingDeprecationWarning", "ImportWarning", "ResourceWarning",
})

# Explicit, reviewed exceptions: {(relpath, lineno): "why this call site
# stays a raw warnings.warn in a silent-by-default category"}. Empty
# today -- #6143 converted every known site to `logger.warning`. Add an
# entry ONLY in the same PR that introduces the call site it covers; the
# reason lives HERE (machine-checked), not in a code comment (which is
# not).
_EXCEPTION_TABLE: "dict[tuple[str, int], str]" = {}


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked `.py` file under `src/reyn/` -- `git ls-files`, the
    same population source `silent_except_ratchet.py`/`suspected_time_
    dependence_ratchet.py` use, for the same reason: it already excludes
    `.venv/`, `__pycache__/`, and anything gitignored, with no
    hand-maintained exclusion list."""
    proc = subprocess.run(
        ["git", "ls-files", "--", f"{_SCOPE}/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def _is_warnings_warn_call(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr == "warn":
        return True
    if isinstance(f, ast.Name) and f.id == "warn":
        return True
    return False


def _warn_category_name(call: ast.Call) -> "str | None":
    """The literal category name a `warnings.warn(...)` call passes --
    positional arg 2, or a `category=` keyword. `None` when omitted (the
    default category is UserWarning, not silent) or when the value is not
    a bare `Name`/`Attribute` this script can read statically (see module
    docstring's disclosed false-reject)."""
    node: "ast.expr | None"
    if len(call.args) >= 2:
        node = call.args[1]
    else:
        node = next((kw.value for kw in call.keywords if kw.arg == "category"), None)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def silent_category_sites(path: Path, root: Path = _ROOT) -> "list[tuple[str, int, str]]":
    """`[(relpath, lineno, category)]` for every `warnings.warn(...)` call
    in *path* whose category is one of `_SILENT_BY_DEFAULT_CATEGORIES`."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    relpath = str(path.relative_to(root))
    out: "list[tuple[str, int, str]]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_warnings_warn_call(node):
            category = _warn_category_name(node)
            if category in _SILENT_BY_DEFAULT_CATEGORIES:
                out.append((relpath, node.lineno, category))
    return out


def measured(root: Path = _ROOT) -> "list[tuple[str, int, str]]":
    sites: "list[tuple[str, int, str]]" = []
    for path in _iter_scan_files(root):
        sites.extend(silent_category_sites(path, root))
    return sites


def unexplained(
    sites: "list[tuple[str, int, str]]",
    table: "dict[tuple[str, int], str] | None" = None,
) -> "list[tuple[str, int, str]]":
    """Sites with no matching exception-table entry -- what makes the
    gate red. *table* defaults to the module's own `_EXCEPTION_TABLE`;
    overridable so tests can exercise the arithmetic against a table that
    is not the (currently empty) shipped one."""
    active_table = _EXCEPTION_TABLE if table is None else table
    return [s for s in sites if (s[0], s[1]) not in active_table]


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])


def main(argv: "list[str] | None" = None) -> int:
    build_parser().parse_args(argv)

    sites = measured()
    bad = unexplained(sites)
    if bad:
        print(
            f"silent-warnings-category gate: {len(bad)} warnings.warn(...) "
            "call(s) under src/reyn/ use a category Python ignores by "
            "default outside __main__ (DeprecationWarning / "
            "PendingDeprecationWarning / ImportWarning / ResourceWarning), "
            "with no entry in scripts/silent_warnings_category_gate.py's "
            "own _EXCEPTION_TABLE:\n",
            file=sys.stderr,
        )
        for relpath, lineno, category in bad:
            print(f"  {relpath}:{lineno}  {category}", file=sys.stderr)
        print(
            "\nEither promote this call to logger.warning (or a stronger "
            "surface an operator actually sees) in the same PR, or add "
            '(relpath, lineno): "<why this stays silent>" to '
            "_EXCEPTION_TABLE in this same PR. See #6143.",
            file=sys.stderr,
        )
        return 1

    print(
        f"silent-warnings-category gate OK: {len(sites)} silent-by-default "
        "category site(s), all in the exception table."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
