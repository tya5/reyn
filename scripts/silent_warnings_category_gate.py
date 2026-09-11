#!/usr/bin/env python3
"""#6143/#6144 -- a `src/reyn/**` population gate over EVERY
`warnings.warn(...)` call site, category-blind.

## Why this exists

#6143's own measurement:

    $ python3 -c "import warnings; [print(f) for f in warnings.filters[:5]]"
    ('default', None, DeprecationWarning, '__main__', 0)
    ('ignore',  None, DeprecationWarning, None, 0)          <- not __main__
    ('ignore',  None, PendingDeprecationWarning, None, 0)
    ('ignore',  None, ImportWarning, None, 0)
    ('ignore',  None, ResourceWarning, None, 0)

Every module under `src/reyn/` is NOT `__main__`, so
DeprecationWarning/PendingDeprecationWarning/ImportWarning/
ResourceWarning fired from `src/reyn/**` reaches nobody in a real run.
The same shape was hit 3 times in one night (#6132, #6137, #6141).

## v1 -> v2: the category axis was itself a false floor (#6144 co-vet round 2)

This gate's FIRST revision only flagged the 4 silent-by-default
categories above. lead-coder's own review caught the gap: swapping
`DeprecationWarning` -> `UserWarning` in any one of those call sites
turns the gate green with the operator's actual visibility unchanged by
one bit. `UserWarning` is not "loud" in the sense that matters here --
architect's own real measurement: `stderr: False / reyn.log: True`. It
reaches `reyn.log` (unlike a silent-by-default category, which reaches
NOTHING), but never an interactive operator's screen directly. Neither
is genuinely "the operator sees this."

The axis this gate checks is therefore CATEGORY-BLIND: every
`warnings.warn(...)` call under `src/reyn/**`, regardless of category,
is population. lead-coder's own framing: "the only legitimate reader of
a raw `warnings.warn` is a library consumer who adds their own `-W`
flag" -- `src/reyn/` is reyn's own application code, not a library
surface reyn's own operators consult with `-W`, so a raw `warnings.warn`
call here is presumptively the wrong tool for reaching an operator,
independent of which category it names.

## What this gate checks

Every `warnings.warn(...)` call under `src/reyn/**` must appear in this
script's own `_EXCEPTION_TABLE` with a reason -- or the gate is RED.
There is no other escape hatch (no comment-based suppression): a
genuinely developer-only warning (there IS a legitimate reader --
someone importing `reyn` as a library and running their own process
under `-W`) stays reviewed, in one place, not decided ad hoc at each
call site.

This is a TABLE, not a ratchet (contrast `silent_except_ratchet.py`,
#5990). #6143 fixed and DELETED all 7 originally-flagged sites (6
promoted to `logger.warning`; the 7th, `permissions.py`'s legacy
`http.get` compat notice, was deleted outright in #6144's own co-vet
round 2 -- it duplicated a REAL, already-firing approval prompt on a
second, noisier channel). Widening the axis in the SAME PR newly
surfaces 18 pre-existing sites (all `UserWarning`, or the default
omitted category) that #6143's own dispatch never covered and #6144's
own review never audited message-by-message -- see #6145, the tracking
issue for promoting each with the SAME wording rigor #6144's own
`:2069` mistake demanded (changing the channel without checking the
wording is a NEW bug, not a fix). Those 18 are DISCLOSED table entries
referencing #6145, not silently declared "legitimate" -- lead-coder's
own framing above says none of them currently are.

## Deliberately narrow call-site resolution (disclosed, not solved)

Detection matches `warnings.warn(...)` / `warn(...)` (a bare imported
name) syntactically via `ast.walk` -- no dataflow, no alias tracking of
a renamed import (`import warnings as w; w.warn(...)` would not match).
The same "suspected, not confirmed" posture `suspected_time_dependence_
ratchet.py` (#4846) and `silent_except_ratchet.py` (#5990) already carry
for a syntax-only AST gate.

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

# #6145 tracks promoting each of these 18 pre-existing sites (all
# UserWarning, or the default omitted category) with the SAME
# message-content audit #6144's own `:2069` round taught is required --
# a channel change alone is not a fix if the wording is wrong. DISCLOSED
# debt, not a claim these are fine as-is (lead-coder's own framing: the
# only legitimate raw `warnings.warn` reader is a `-W`-flagged library
# consumer, which none of these are).
_EXCEPTION_TABLE: "dict[tuple[str, int], str]" = {
    ("src/reyn/config/chat.py", 1451): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/oauth.py", 150): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/oauth.py", 159): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/oauth.py", 167): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/oauth.py", 196): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/oauth.py", 205): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/oauth.py", 250): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/loader.py", 47): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/loader.py", 56): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/loader.py", 79): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/loader.py", 88): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/loader.py", 133): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/loader.py", 143): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/security/secrets/interpolation.py", 37): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/plugins/tokens.py", 310): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/mcp/client.py", 2523): "#6145 -- pre-#6143, omitted category (defaults to UserWarning), not yet audited",
    ("src/reyn/hooks/composer.py", 634): "#6145 -- pre-#6143 UserWarning, not yet audited",
    ("src/reyn/hooks/composer.py", 838): "#6145 -- pre-#6143 UserWarning, not yet audited",
}


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
    implicit default is `UserWarning`) or when the value is not a bare
    `Name`/`Attribute` this script can read statically. Retained for the
    reported category label only -- v2's own population no longer
    filters by this value (see module docstring, "v1 -> v2")."""
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
    """`[(relpath, lineno, category)]` for EVERY `warnings.warn(...)`
    call in *path*, category-blind (v2, #6144 co-vet round 2 -- see
    module docstring's "v1 -> v2"). `category` in the returned tuple is
    `"UserWarning"` when omitted or unresolved, purely for the report
    label -- it is never used to decide membership."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    relpath = str(path.relative_to(root))
    out: "list[tuple[str, int, str]]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_warnings_warn_call(node):
            category = _warn_category_name(node) or "UserWarning"
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
    is not the shipped one."""
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
            "call(s) under src/reyn/ (any category) have no entry in "
            "scripts/silent_warnings_category_gate.py's own "
            "_EXCEPTION_TABLE:\n",
            file=sys.stderr,
        )
        for relpath, lineno, category in bad:
            print(f"  {relpath}:{lineno}  {category}", file=sys.stderr)
        print(
            "\nEither promote this call to logger.warning (or a stronger "
            "surface an operator actually sees) in the same PR, or add "
            '(relpath, lineno): "<why this stays a raw warnings.warn, '
            'tracked how>" to _EXCEPTION_TABLE in this same PR. See '
            "#6143/#6144/#6145.",
            file=sys.stderr,
        )
        return 1

    print(
        f"silent-warnings-category gate OK: {len(sites)} warnings.warn(...) "
        "site(s), all in the exception table."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
