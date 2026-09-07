#!/usr/bin/env python3
"""#5949 stage ①-b, architect's structural ③ (issuecomment-5576144700) —
the SYNTACTIC backstop for ``Session._parse_history_line``'s ``hydrate``
flag, the flip that made "materialize a content_ref row's body" an
explicit, required, per-caller decision instead of a silent default.

## The class this closes

Before this: ``preview_only: bool = False`` meant "materialize by
default" — a NEW caller of ``_parse_history_line`` that didn't think about
cost landed on the expensive path automatically, and nothing anywhere
would ever tell it that. This is exactly what happened once: stage ①
fixed ONE of ``_parse_history_line``'s callers
(``Session._load_older_entries``) and never touched its sibling
(``Session.extend_history_backward_async``, which had its own SEPARATE
parse loop) — owner's real backward-page-heavy path kept eager-
materializing every content_ref body, unchanged, and the regression
shipped silently (TESTS-READ, CI green, merged).

``hydrate`` now has NO DEFAULT — Python itself already turns "a new
caller forgot to decide" into an immediate ``TypeError``. What Python's
own required-kwarg mechanism does NOT defend against is the OPPOSITE
mistake: a new (or edited) caller passing ``hydrate=True`` out of
caution/laziness, silently re-opening the exact cost this flip exists to
close, with no error anywhere — passing *some* value always satisfies the
signature. This gate is the SYNTACTIC (not semantic) backstop for that
one architect ruling: "本体が実際に要るのは wire を組む地点だけ" — the
codebase should carry EXACTLY ONE ``hydrate=True`` call site
(compaction's own token-measurement read, the one caller that reads
``.content`` directly rather than through
``RouterHistoryBuffer._serialise_turn``'s own unconditional
``resolve_history_content`` call).

## Two independent checks, not one

Per architect's own post-mortem on THIS SAME PR's co-vet (issuecomment-
5576144700): "flag の grep は「渡した場所」を数えるだけで、「渡していない
呼び手」を数えない" — counting ``hydrate=True`` occurrences alone would
have caught a wrong VALUE at an already-updated call site, but not a
call site that was never updated in the first place (a 6th caller added
later, or one this migration missed). So this gate runs BOTH:

1. **Population**: every REAL ``_parse_history_line(...)`` call
   (``ast.Call``, not a comment or a docstring's own prose mention) must
   pass ``hydrate`` as a keyword argument — a call site with none would
   be a ``TypeError`` at runtime already, but this gate catches it at
   review time, in the diff, before a test happens to exercise that
   exact line. AST-based (not a text/regex match) specifically so a
   docstring sentence like "the ``self._parse_history_line(line)``
   call" or a comment mentioning the method name never counts as a
   call site — #5949 stage ①-b's own review hit exactly this shape.
2. **Exactly-one**: ``hydrate=True`` (a literal ``True``, not merely a
   truthy expression — this gate does not attempt to evaluate a
   variable/expression argument, see :func:`_is_call_to` below) appears
   at EXACTLY ONE call site across ``src/`` — today, ``Session.
   _durable_active_history_after``'s own compaction-candidate read. A
   count of 0 means that caller regressed to a lazy-only read (silently
   wrong for compaction's token estimates); a count of 2+ means a NEW
   caller chose the expensive path without anyone deciding it should.

Both checks are pure syntax (parsed, never executed) — zero false
positives by construction, the same "構文なら zero-FP" class architect
named for #4846's own gate (``check_pr_closing_intent.py``'s own kind of
check).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("src", "tests")
_METHOD_NAME = "_parse_history_line"


def _iter_py_files(root: Path, scan_dirs: "tuple[str, ...]" = _SCAN_DIRS) -> "list[Path]":
    files: "list[Path]" = []
    for scan_dir in scan_dirs:
        d = root / scan_dir
        if d.is_dir():
            files.extend(sorted(d.rglob("*.py")))
    return files


def _is_call_to(node: ast.AST, name: str) -> bool:
    """True if *node* is a ``Call`` whose callee's final attribute/name
    is *name* — matches ``self._parse_history_line(...)``,
    ``session._parse_history_line(...)``, or a bare
    ``_parse_history_line(...)``, never a string, comment, or docstring
    mention of the same text."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == name
    if isinstance(func, ast.Name):
        return func.id == name
    return False


def find_calls_missing_hydrate(
    root: Path = _ROOT, scan_dirs: "tuple[str, ...]" = _SCAN_DIRS,
) -> "list[tuple[Path, int]]":
    """Every REAL ``_parse_history_line(...)`` call site (excluding the
    ``def`` line itself) that does not pass ``hydrate`` as a keyword
    argument. Isolated from CLI/printing so it is directly testable
    (``root`` defaults to the real repo root; a test passes its own
    fixture tree)."""
    offenders: "list[tuple[Path, int]]" = []
    for path in _iter_py_files(root, scan_dirs):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        def_body_ids: "set[int]" = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == _METHOD_NAME:
                def_body_ids.update(id(n) for n in ast.walk(node))
        for node in ast.walk(tree):
            if id(node) in def_body_ids:
                continue  # inside the def's own body -- not a caller
            if not isinstance(node, ast.Call) or not _is_call_to(node, _METHOD_NAME):
                continue
            has_hydrate = any(kw.arg == "hydrate" for kw in node.keywords)
            if not has_hydrate:
                offenders.append((path, node.lineno))
    return offenders


def count_hydrate_true_in_src(root: Path = _ROOT) -> "tuple[int, list[tuple[Path, int]]]":
    """The population of REAL ``hydrate=True`` call sites under ``src/``
    only (a test file legitimately drives either value for direct unit
    coverage — the exactly-one constraint is a PRODUCTION cost claim,
    architect's "wire を組む地点だけ", not a test-authoring rule). Only a
    LITERAL ``True`` counts — a variable or expression argument is not
    evaluated (this gate is a static syntax check, not an interpreter);
    such a call site would also be flagged by nothing here, which is a
    disclosed gap, not a silent one (see this function's own caller).
    ``root`` defaults to the real repo root; a test passes its own
    fixture tree."""
    hits: "list[tuple[Path, int]]" = []
    src_dir = root / "src"
    if not src_dir.is_dir():
        return 0, hits
    for path in sorted(src_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_call_to(node, _METHOD_NAME):
                continue
            for kw in node.keywords:
                if kw.arg == "hydrate" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    hits.append((path, node.lineno))
    return len(hits), hits


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-tree scan against the current population
    missing = find_calls_missing_hydrate()
    true_count, true_hits = count_hydrate_true_in_src()

    ok = True

    if missing:
        ok = False
        print("parse-history-line-hydrate gate FAILED (population):\n", file=sys.stderr)
        print(
            f"{len(missing)} call(s) to _parse_history_line(...) do not pass "
            "hydrate= as a keyword argument — every caller must decide "
            "(#5949 stage ①-b, architect's structural ruling):",
            file=sys.stderr,
        )
        for path, lineno in missing:
            rel = path.relative_to(_ROOT)
            print(f"  {rel}:{lineno}", file=sys.stderr)

    if true_count != 1:
        ok = False
        print(
            f"\nparse-history-line-hydrate gate FAILED (exactly-one): "
            f"expected exactly 1 hydrate=True call site under src/, found "
            f"{true_count}:",
            file=sys.stderr,
        )
        for path, lineno in true_hits:
            rel = path.relative_to(_ROOT)
            print(f"  {rel}:{lineno}", file=sys.stderr)
        print(
            "\nThe one expected site is Session._durable_active_history_"
            "after's own compaction-candidate read (_measure_and_select "
            "computes real token estimates directly over .content, before "
            "any wire is built). A count of 0 means that caller regressed "
            "to a lazy-only read; 2+ means a new caller chose the "
            "expensive path without anyone deciding it should — either "
            "way, update this gate's own docstring if the population "
            "genuinely changed on purpose, don't just raise the number.",
            file=sys.stderr,
        )

    if ok:
        print(
            "OK: every _parse_history_line(...) call names hydrate= "
            "explicitly, and exactly 1 call site under src/ passes "
            "hydrate=True."
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
