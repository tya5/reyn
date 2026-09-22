#!/usr/bin/env python3
"""#6234 gate — every ``TextualChatApp._record_pump_swallow`` call site's
first positional argument (``site``) must be a literal string, and no two
call sites may pass the SAME literal.

## Why this exists

Architect ruling (#6234, issuecomment-5772189312): ``PumpSwallowStats``
dedups a caught exception by ``(site, exception type)`` — the FIRST time a
given pair is seen, an audit-event is durably recorded; every repeat only
bumps a count. ``site`` is a hand-written literal (not derived from a
detector — #6234's own "3 kinds of key" classification puts a static,
self-naming literal in the "own, unique by construction" bucket, which
needs no detector EXCEPT for the one real risk a *hand-written* literal
carries: a copy-paste duplicate). If two call sites pass the same
``site`` string, they SHARE one dedup bucket: whichever fires first
silently suppresses the audit-event for the other, forever, for that
process's lifetime — the exact "N grows but WHERE stays hidden" failure
this counter exists to prevent (see ``PumpSwallowStats``'s own docstring).

## Scope: structural, zero-FP (same shape as check_tui_widget_boundary.py)

This gate answers ONE question per call site: is the first positional
argument to ``_record_pump_swallow`` a literal ``str`` constant, and are
all such constants — across every call site in the file — pairwise
distinct? Both are decidable exactly from the AST (a `Constant` node
either IS a string literal or it ISN'T; string equality is unambiguous),
so this is a PIN, not a ratchet — there is no grandfathered baseline and
none is ever expected. A non-literal first argument (a variable, an
f-string, a method call) is ALSO a finding — the whole point of ``site``
being a literal is that no runtime value can accidentally collide two
call sites without the sole author noticing the duplicated string in the
diff.

CI: gate
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGET = (
    _ROOT
    / "src"
    / "reyn"
    / "interfaces"
    / "inline"
    / "textual_chat"
    / "app.py"
)

_METHOD_NAME = "_record_pump_swallow"


def _call_sites(tree: "ast.Module") -> "list[ast.Call]":
    """Every ``Call`` node invoking ``self._record_pump_swallow(...)`` —
    matched on the attribute NAME only (not a specific ``self`` binding),
    same shape ``check_tui_widget_boundary.py`` uses for import-prefix
    matching: a real, unambiguous AST shape, never a substring search over
    the file's text (a docstring MENTIONING the method name must not
    false-positive here — and several already do, in this very file)."""
    sites: "list[ast.Call]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == _METHOD_NAME:
            sites.append(node)
        elif isinstance(func, ast.Name) and func.id == _METHOD_NAME:
            sites.append(node)
    return sites


def find_violations(
    target: "Path | None" = None,
) -> "tuple[list[tuple[int, str]], list[int]]":
    """Returns ``(duplicates, non_literal_lines)``.

    ``duplicates`` is ``(lineno, site)`` for every call site whose literal
    ``site`` string is NOT the first occurrence of that string in the
    file — i.e. every collision beyond the first. ``non_literal_lines`` is
    every call site's line number whose first positional argument is not
    a plain string literal (also a finding: this gate's whole premise —
    "collisions are visible in the diff" — requires the argument to BE a
    literal in the first place).

    ``target`` defaults to ``None`` — resolved to the module-level
    ``_TARGET`` INSIDE the function body (a name lookup at CALL time), not
    a bound default-argument value — same reason ``check_tui_widget_
    boundary.py``'s own ``find_violations`` gives: a test monkeypatching
    ``_TARGET`` on this module must reach this function too."""
    if target is None:
        target = _TARGET
    tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))

    seen: "dict[str, int]" = {}
    duplicates: "list[tuple[int, str]]" = []
    non_literal_lines: "list[int]" = []

    for call in _call_sites(tree):
        if not call.args:
            non_literal_lines.append(call.lineno)
            continue
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            site = first.value
            if site in seen:
                duplicates.append((call.lineno, site))
            else:
                seen[site] = call.lineno
        else:
            non_literal_lines.append(call.lineno)

    return duplicates, non_literal_lines


def main() -> int:
    if not _TARGET.is_file():
        print(
            f"check_pump_swallow_site_uniqueness: {_TARGET} not found",
            file=sys.stderr,
        )
        return 1

    duplicates, non_literal_lines = find_violations(_TARGET)
    if not duplicates and not non_literal_lines:
        print(
            "check_pump_swallow_site_uniqueness OK: every "
            f"{_METHOD_NAME} call site passes a distinct string literal."
        )
        return 0

    try:
        shown = _TARGET.relative_to(_ROOT)
    except ValueError:
        shown = _TARGET  # a fixture path outside _ROOT (test-only) — show it as-is

    print("check_pump_swallow_site_uniqueness FAILED:\n", file=sys.stderr)
    for lineno, site in duplicates:
        print(
            f"  {shown}:{lineno} reuses site {site!r} — "
            "already used by an earlier call site; two call sites sharing "
            "one `site` collapse into one dedup bucket, silently "
            "suppressing the audit-event for whichever fires second "
            "(#6234). Give this call site its own literal.",
            file=sys.stderr,
        )
    for lineno in non_literal_lines:
        print(
            f"  {shown}:{lineno} passes a non-literal "
            f"first argument to {_METHOD_NAME} — `site` must be a plain "
            "string literal so a duplicate is visible in the diff (#6234).",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
