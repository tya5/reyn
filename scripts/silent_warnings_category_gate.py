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

## v2 -> v3: the key needed a THIRD coordinate (#6144 co-vet round 3)

v2 keyed `_EXCEPTION_TABLE` on `(relpath, lineno)` alone. lead-coder's
own review caught the gap this reopens: a call site this gate exists to
CATCH does not need to dodge the gate at all if it simply lands on the
same line number an already-exempted site used to occupy (the exempted
site moved down, a new one took its old line -- an everyday consequence
of an unrelated edit above it in the same file). Position alone is not
an identity; two DIFFERENT `warnings.warn` calls can share one. "Keep
line numbers stable by convention" was explicitly rejected -- an
invariant held only by someone's habit, never enforced, is not actually
held.

The key is therefore `(relpath, lineno, message_template)`, where
`message_template` is the call's own message argument with every
f-string expression normalised to a fixed `"{}"` placeholder (see
`_message_template` below) and truncated to `_TEMPLATE_LEN` characters.
A table entry now only matches a call whose own message-shape is a
prefix-equal match, not merely one sharing a line number -- an
exempted site moving down is still caught (a DIFFERENT lineno with the
SAME template has no matching key: fail-loud, the same "moved = red,
and that's fine" contract v2 already had for `(relpath, lineno)`), and
a genuinely new, unaudited `warnings.warn` landing on an old exempted
site's line number no longer inherits that exemption silently -- its
own message template almost certainly differs, so the composite key
misses and the gate goes red for it specifically.

## What this gate checks

Every `warnings.warn(...)` call under `src/reyn/**` must appear in this
script's own `_EXCEPTION_TABLE` (by its full 3-part key) with a reason
-- or the gate is RED. There is no other escape hatch (no comment-based
suppression): a genuinely developer-only warning (there IS a legitimate
reader -- someone importing `reyn` as a library and running their own
process under `-W`) stays reviewed, in one place, not decided ad hoc at
each call site.

This is a TABLE, not a ratchet (contrast `silent_except_ratchet.py`,
#5990). #6143 fixed and DELETED all 7 originally-flagged sites (6
promoted to `logger.warning`; the 7th, `permissions.py`'s legacy
`http.get` compat notice, was deleted outright in #6144's own co-vet
round 2 -- it duplicated a REAL, already-firing approval prompt on a
second, noisier channel). Widening the axis in the SAME PR newly
surfaced 18 pre-existing sites (all `UserWarning`, or the default
omitted category) that #6143's own dispatch never covered and #6144's
own review never audited message-by-message -- tracked by #6145.
#6145 individually audited each of the 18 (file:line, reason -- see
that PR's own body for the full table) and promoted all 18 to
`logger.warning`, the SAME wording rigor #6144's own `:2069` mistake
demanded (changing the channel without checking the wording is a NEW
bug, not a fix). `_EXCEPTION_TABLE` is empty again as of #6145
landing -- there is currently no site under `src/reyn/**` this gate
considers a legitimate raw `warnings.warn`.

## Deliberately narrow call-site / template resolution (disclosed, not solved)

Detection matches `warnings.warn(...)` / `warn(...)` (a bare imported
name) syntactically via `ast.walk` -- no dataflow, no alias tracking of
a renamed import (`import warnings as w; w.warn(...)` would not match).
`_message_template` reads only a literal string / f-string (`ast.
Constant` / `ast.JoinedStr`) message argument; a message built any other
way (a variable, a function call, string `%`/`.format()` composed
elsewhere and passed in) resolves to `""` -- an EMPTY template still
participates in the 3-part key (so it can still be exempted, keyed on
`(relpath, lineno, "")`), but offers no discrimination against a
different such call landing on the same line, which is the same
disclosed gap `suspected_time_dependence_ratchet.py` (#4846) and
`silent_except_ratchet.py` (#5990) already carry for a syntax-only AST
gate -- the table is empty as of #6145 (see below), so this gap is
currently unexercised by any real site; it stays disclosed for
whichever future entry lands first.

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

#: Message-template truncation length. Long enough that two genuinely
#: different messages at the same call site are exceedingly unlikely to
#: share a prefix this long; short enough that a cosmetic wording tweak
#: elsewhere in a long message (not in its first _TEMPLATE_LEN chars)
#: does not force a table-entry churn unrelated to this gate's purpose.
_TEMPLATE_LEN = 40

# #6145: the 18 pre-existing sites this table used to carry as disclosed,
# unaudited debt (see #6144's own final commit for that table) were each
# individually audited -- 18/18 classified "A: should reach the operator"
# and promoted to `logger.warning` (see #6145's own PR body for the
# file:line / reason table). None were classified "B: raw warnings.warn
# is the right tool" or "C: delete -- a real duplicate exists elsewhere"
# (lead-coder's own framing at the time this table was 18-strong: none of
# these 18 were library-consumer-only surfaces to begin with). Population
# is 0 as of #6145 landing -- back to a TABLE, not a ratchet, exactly the
# state #6144 left it in immediately after promoting its own 7 (see
# module docstring's "What this gate checks").
#
# The 3rd element of a future entry's key would be that call's own
# message TEMPLATE (see `_message_template`'s own docstring) -- a new,
# unaudited `warnings.warn` landing on an old exempted site's line number
# would NOT inherit that exemption unless its message also happens to
# share the same ~40-char prefix (#6144 co-vet round 3).
_EXCEPTION_TABLE: "dict[tuple[str, int, str], str]" = {}


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
    reported category label only -- population membership never filters
    by this value (see module docstring, "v1 -> v2")."""
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


def _message_template(call: ast.Call, length: int = _TEMPLATE_LEN) -> str:
    """The message argument's own TEMPLATE -- every f-string expression
    normalised to a fixed `"{}"` placeholder, truncated to *length*
    characters (see module docstring, "v2 -> v3"). `""` when the message
    argument is missing or not a literal string / f-string this script
    can read statically (a variable, a function call, `%`/`.format()`
    composition) -- a disclosed, not solved, limit; see module
    docstring's own final section."""
    if not call.args:
        return ""
    node = call.args[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value[:length]
    if isinstance(node, ast.JoinedStr):
        parts: "list[str]" = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{}")
        return "".join(parts)[:length]
    return ""


def silent_category_sites(path: Path, root: Path = _ROOT) -> "list[tuple[str, int, str, str]]":
    """`[(relpath, lineno, category, message_template)]` for EVERY
    `warnings.warn(...)` call in *path*, category-blind (v2, #6144
    co-vet round 2 -- see module docstring's "v1 -> v2"). `category` is
    `"UserWarning"` when omitted or unresolved, purely for the report
    label -- it is never used to decide membership. `message_template`
    is the 3rd key coordinate `unexplained` matches against (v3, #6144
    co-vet round 3)."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    relpath = str(path.relative_to(root))
    out: "list[tuple[str, int, str, str]]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_warnings_warn_call(node):
            category = _warn_category_name(node) or "UserWarning"
            template = _message_template(node)
            out.append((relpath, node.lineno, category, template))
    return out


def measured(root: Path = _ROOT) -> "list[tuple[str, int, str, str]]":
    sites: "list[tuple[str, int, str, str]]" = []
    for path in _iter_scan_files(root):
        sites.extend(silent_category_sites(path, root))
    return sites


def unexplained(
    sites: "list[tuple[str, int, str, str]]",
    table: "dict[tuple[str, int, str], str] | None" = None,
) -> "list[tuple[str, int, str, str]]":
    """Sites with no matching exception-table entry -- what makes the
    gate red. Matches the FULL 3-part key `(relpath, lineno,
    message_template)` (#6144 co-vet round 3) -- a site sharing only a
    line number with a table entry, but not its message template, is
    NOT explained by it. *table* defaults to the module's own
    `_EXCEPTION_TABLE`; overridable so tests can exercise the arithmetic
    against a table that is not the shipped one."""
    active_table = _EXCEPTION_TABLE if table is None else table
    return [s for s in sites if (s[0], s[1], s[3]) not in active_table]


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
        for relpath, lineno, category, template in bad:
            print(f"  {relpath}:{lineno}  {category}  {template!r}", file=sys.stderr)
        print(
            "\nEither promote this call to logger.warning (or a stronger "
            "surface an operator actually sees) in the same PR, or add "
            '(relpath, lineno, message_template): "<why this stays a raw '
            'warnings.warn, tracked how>" to _EXCEPTION_TABLE in this same '
            "PR. See #6143/#6144/#6145.",
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
