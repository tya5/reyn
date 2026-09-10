#!/usr/bin/env python3
"""Recurrence-prevention gate for the caplog-consumption bug pattern that hit
#6031, #6035, and #6038 independently, the same day (2026-09-10): a test
asserts something about `caplog.records`/`caplog.messages`/`caplog.text`
WITHOUT filtering to the test's own subject (logger name or message
content) first. `caplog`'s handler is attached to the ROOT logger for the
whole pytest-xdist worker process, not scoped to one test — under `-n
auto`, an unrelated test's background record on a DIFFERENT logger (a
leaked async client's finalizer warning, a lingering timer) can land inside
the window and flip an unfiltered assertion. This is flaky-by-construction,
never a real invariant about the subject under test.

## Read-only measurement that justified building this (this session)

A hand-written AST walk over every `caplog.records`/`.messages`/`.text`
consumption site in `tests/**/*.py` found 112 total sites: 108 already
subject-specific (a membership/containment check, or a comprehension/
`any()`/`all()` predicate filtering on message content or logger name
before anything is counted, compared-empty, or unpacked) and 4 genuinely
unfiltered -- the 3 incidents above (each independently fixed in its own
PR before this gate existed) plus 2 that were still live at measurement
time: `tests/runtime/test_5509_media_capability_gate.py:220` and
`tests/interfaces/test_5168_tui_stdio_capture.py:118` (fixed in the SAME
PR that adds this gate). Zero false positives were found in that
measurement -- no site among the 4 had a test-isolation marker, a comment,
or any other reason "assert nobody logged, from any subsystem" was the
actual intent.

## What counts as DANGEROUS (the population this gate targets)

The raw `caplog.records`/`.messages`/`.text` collection (or a name it was
assigned to, untouched) feeding DIRECTLY into one of three shapes, with no
subject-specific check applied anywhere along the way:

1. an equality/inequality comparison against a literal (`caplog.records ==
   []`, `caplog.text == ""`, `messages != []`) -- `in`/`not in` is exempt
   (a containment check against a literal string IS the subject-specific
   check, by construction: `"foo" in caplog.text`).
2. a `len(...)` comparison (`len(caplog.records) == 2`).
3. tuple/sequence unpacking (`(only,) = caplog.records`, `a, b = warnings`).
4. an `any()`/`all()` call whose generator has no subject-specific
   predicate (`not any(r.levelno >= logging.WARNING for r in
   caplog.records)` -- semantically "no record from ANYONE at that level",
   the exact #5509 shape this PR fixes) -- listed separately from 1-3
   because syntactically it is a call, not a comparison, but it is the
   SAME "assert nobody logged" hazard: `any()`/`all()` collapses a
   generator down to one bool the same way `== []` collapses a list.

A "subject-specific check" is: a reference to a record's `.name` (logger
name) or `.message`/`.getMessage()` (message content), OR an `in`/`not in`
comparison against a literal (content match) -- anywhere inside the
consuming expression's own filter (a comprehension's `if` clauses, or a
generator-expression's element when it is the sole argument to
`any()`/`all()`), or already baked into a variable the expression was
assigned from (`records = [r for r in caplog.records if r.name == X]`
then `records == []]` is SAFE -- the filter already ran).

## What this gate does NOT attempt (disclosed, not solved)

This is a **syntax-shape** classifier (`ast.walk`, no dataflow, no type
inference), same posture `suspected_time_dependence_ratchet.py` (#4846)
and `silent_except_ratchet.py` (#5990) already established for this repo's
AST gates -- "suspected", not "confirmed". Two known gaps, found by hand
while building this gate, deliberately left OUT of its population:

- **Bare truthiness** (`assert not caplog.records`, `assert caplog.records
  == None`-style `if caplog.records:`) is the SAME hazard shape as #1
  above (an unrelated record makes a non-empty list truthy) but is not
  one of the three named consumption shapes the read-only measurement
  scoped this gate to, and is NOT what any of #6031/#6035/#6038 looked
  like. At least 3 live examples exist today (`tests/hooks/
  test_hook_shell_push_2069.py:205`, `tests/runtime/
  test_5982_internal_boundary_fold.py:190`, `tests/security/
  test_sandbox_capability_declaration_4935.py:205`) -- named here, not
  fixed here (out of this PR's assigned scope), and NOT counted toward
  this gate's population or its baseline-less pass/fail. A future PR
  widening the classifier to cover this shape is a deliberate, reviewed
  decision to make separately -- not something this gate silently grows
  into.
- Variable-derivation tracing is single-hop (`x = <expr>`, then `x` used
  in a dangerous shape) and does not follow a value through more than one
  reassignment, a function call, or a non-`Name` target. A pattern that
  launders an unfiltered collection through an intermediate helper
  function before comparing it would not be caught.

## `caplog.text` scope decision (explicit, per lead-coder's requirement)

All 10 `caplog.text` sites in the tree TODAY are safe (`"foo" in
caplog.text` / `"foo" not in caplog.text` -- containment against a literal,
which is subject-specific by construction). The identical hazard shape
IS structurally possible for `.text` (`caplog.text == ""`, `len(caplog.
text) == 0`) but zero real examples exist right now. This gate does not
special-case `.text` beyond the general consumption-shape rule above --
`.text`, `.records`, and `.messages` are classified identically, so a
future dangerous `.text` site is already covered by the same rule that
covers `.records`/`.messages` today. No speculative handling was written
for a case with 0 population (this repo's discipline has burned on that
exact shape twice already: #4846, #5990) -- if one appears, the general
rule catches it; nothing here needs updating first.

## False-positive / recall disclosure (condition 4, explicit)

False-positive rate is measured at ~0: this gate's classifier, run
against the 108 known-safe sites from the read-only measurement, flags
none of them. Recall is NOT claimed complete or high -- it is verified
ONLY against the 3 known incident shapes (#6031/#6035/#6038) plus the 2
sites this PR fixes, all 5 of which it correctly flags before their fix
and clears after. Whether it would catch every FUTURE unfiltered-caplog
shape is unmeasured and not claimed.

## No baseline / grandfather list (explicit, per lead-coder's constraint)

This is a zero-tolerance gate, not a ratchet: no baseline JSON, no
`--write-baseline` escape hatch. The tree is clean today (both remaining
live sites are fixed in this same PR) -- any hit is a NEW regression, not
inherited debt to be silenced.

CI: gate
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCOPE = "tests"

_CAPLOG_ATTRS = frozenset({"records", "messages", "text"})
_SUBJECT_ATTRS = frozenset({"name", "message"})


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked `.py` file under `tests/` -- `git ls-files`, the same
    population source `silent_except_ratchet.py`/`check_no_core_dep_
    importorskip.py` use, for the same reason: no hand-maintained
    exclusion list, and it already excludes anything gitignored."""
    proc = subprocess.run(
        ["git", "ls-files", "--", f"{_SCOPE}/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def _is_caplog_access(node: "ast.AST | None") -> bool:
    """True for a real `caplog.records`/`caplog.messages`/`caplog.text`
    attribute access -- an `ast.Attribute` node, never a text/regex match,
    so a docstring or comment merely naming the pattern cannot appear
    here."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _CAPLOG_ATTRS
        and isinstance(node.value, ast.Name)
        and node.value.id == "caplog"
    )


def _contains_caplog_access(node: "ast.AST | None") -> bool:
    if node is None:
        return False
    return any(_is_caplog_access(n) for n in ast.walk(node))


def _references_subject(expr: "ast.AST | None") -> bool:
    """True if *expr*'s subtree contains a subject-specific check: a
    record's `.name` (logger name) or `.message`/`.getMessage()` (message
    content) attribute/call, or an `in`/`not in` comparison against a
    literal (content containment -- `"foo" in caplog.text`, `"foo" in m`
    where `m` is already a message string). Any of these, anywhere in the
    expression, counts -- this is deliberately loose (a syntax-presence
    check, not a proof the reference is load-bearing on the path that
    matters); see module docstring's disclosed-gap section."""
    if expr is None:
        return False
    for n in ast.walk(expr):
        if isinstance(n, ast.Attribute) and n.attr in _SUBJECT_ATTRS:
            return True
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == "getMessage":
                return True
        if isinstance(n, ast.Compare) and any(
            isinstance(op, (ast.In, ast.NotIn)) for op in n.ops
        ):
            return True
    return False


def _comprehension_expr_is_filtered(comp_expr: ast.AST) -> bool:
    """*comp_expr* is a `ListComp`/`SetComp`/`DictComp`/`GeneratorExp`.
    Safe (filtered) iff a subject-specific check appears in an `if`
    clause of any of its generators (the conventional shape for a
    materializing comprehension: `[r for r in caplog.records if
    r.name == X]`), OR in its own element expression (the shape a
    generator handed straight to `any()`/`all()` uses instead: `any(r.name
    == X for r in caplog.records)` -- the element IS the predicate
    there)."""
    generators = getattr(comp_expr, "generators", [])
    for gen in generators:
        if any(_references_subject(cond) for cond in gen.ifs):
            return True
    if isinstance(comp_expr, ast.DictComp):
        return _references_subject(comp_expr.key) or _references_subject(comp_expr.value)
    elt = getattr(comp_expr, "elt", None)
    return _references_subject(elt)


class _FunctionAnalysis:
    """Per-function (or per-module, for top-level code) derivation state:
    which local names were assigned directly from a caplog access or from
    a comprehension over one, and whether that derivation was already
    subject-filtered. Single-hop only (see module docstring)."""

    def __init__(self) -> None:
        self.filtered_names: "set[str]" = set()
        self.unfiltered_names: "set[str]" = set()

    def is_caplog_derived(self, name: str) -> bool:
        return name in self.filtered_names or name in self.unfiltered_names

    def is_filtered_name(self, name: str) -> bool:
        return name in self.filtered_names


def _classify_value_expr(value: ast.AST, analysis: _FunctionAnalysis) -> "bool | None":
    """For *value* (the thing directly feeding a dangerous shape --
    compared, len()'d, or unpacked from), return True if it is already
    subject-filtered (SAFE), False if it is raw/unfiltered (DANGEROUS), or
    None if *value* has nothing to do with caplog at all (not this gate's
    concern)."""
    if _is_caplog_access(value):
        return False  # the raw collection itself, no filter possible here
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        if not _contains_caplog_access(value):
            return None
        return _comprehension_expr_is_filtered(value)
    if isinstance(value, ast.Name):
        if analysis.is_filtered_name(value.id):
            return True
        if analysis.is_caplog_derived(value.id):
            return False
        return None
    return None


def _record_derivation(assign: ast.Assign, analysis: _FunctionAnalysis) -> None:
    """A plain `x = <expr>` (single `Name` target, no unpacking) whose
    value derives from a caplog access updates *analysis* so a later
    direct consumption of `x` can be classified. Unpacking targets are
    handled separately, as a dangerous SHAPE in their own right (see
    `_scan_function`) -- they never reach this function."""
    if len(assign.targets) != 1 or not isinstance(assign.targets[0], ast.Name):
        return
    verdict = _classify_value_expr(assign.value, analysis)
    if verdict is None:
        return
    name = assign.targets[0].id
    (analysis.filtered_names if verdict else analysis.unfiltered_names).add(name)


def _compare_touches_op(compare: ast.Compare, idx: int, op_types: "tuple[type, ...]") -> bool:
    ops_touching = []
    if idx > 0:
        ops_touching.append(compare.ops[idx - 1])
    if idx < len(compare.ops):
        ops_touching.append(compare.ops[idx])
    return any(isinstance(op, op_types) for op in ops_touching)


def _scan_function(func_body: "list[ast.stmt]") -> "list[tuple[int, str]]":
    """`(lineno, shape)` for every dangerous consumption found directly in
    *func_body* (a function's or module's statement list) -- single pass:
    derivations are recorded as they're seen, then every statement is
    also checked for a direct dangerous shape. Because most of this
    repo's real sites assign-then-consume in that order within one
    function, a single top-to-bottom walk suffices; a consume-before-
    assign ordering (unusual) would simply miss the derivation, a
    disclosed single-hop limit, not a crash."""
    analysis = _FunctionAnalysis()
    violations: "list[tuple[int, str]]" = []

    class _Visitor(ast.NodeVisitor):
        def visit_Assign(self, node: ast.Assign) -> None:
            # Unpacking target straight off a caplog-derived value is a
            # dangerous SHAPE (#3), not merely a derivation.
            if any(isinstance(t, (ast.Tuple, ast.List)) for t in node.targets):
                verdict = _classify_value_expr(node.value, analysis)
                if verdict is False:
                    violations.append((node.lineno, "tuple/sequence unpacking"))
            else:
                _record_derivation(node, analysis)
            self.generic_visit(node)

        def visit_Compare(self, node: ast.Compare) -> None:
            values = [node.left, *node.comparators]
            for idx, v in enumerate(values):
                verdict = _classify_value_expr(v, analysis)
                if verdict is None:
                    continue
                if _compare_touches_op(node, idx, (ast.In, ast.NotIn)):
                    continue  # containment IS the subject-specific check
                if _compare_touches_op(node, idx, (ast.Eq, ast.NotEq)) and verdict is False:
                    violations.append((node.lineno, "equality with literal"))
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == "len":
                for arg in node.args:
                    verdict = _classify_value_expr(arg, analysis)
                    if verdict is False:
                        violations.append((node.lineno, "len() comparison"))
            elif (
                isinstance(node.func, ast.Name)
                and node.func.id in ("any", "all")
                and len(node.args) == 1
                and isinstance(node.args[0], ast.GeneratorExp)
            ):
                genexp = node.args[0]
                if _contains_caplog_access(genexp) or any(
                    isinstance(g.iter, ast.Name) and analysis.is_caplog_derived(g.iter.id)
                    for g in genexp.generators
                ):
                    filtered = _comprehension_expr_is_filtered(genexp) or any(
                        isinstance(g.iter, ast.Name) and analysis.is_filtered_name(g.iter.id)
                        for g in genexp.generators
                    )
                    if not filtered:
                        violations.append((node.lineno, f"unfiltered {node.func.id}()"))
            self.generic_visit(node)

    _Visitor().visit(ast.Module(body=func_body, type_ignores=[]))
    return violations


def find_violations(path: Path) -> "list[tuple[int, str]]":
    """`(lineno, shape)` for every unfiltered caplog consumption in
    *path*, scanned function-by-function (and once over any top-level
    module code) so each gets its own derivation state -- a name derived
    in one test function says nothing about a same-named local in
    another."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    violations: "list[tuple[int, str]]" = []
    top_level: "list[ast.stmt]" = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_scan_function(node.body))
        else:
            top_level.append(node)
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    violations.extend(_scan_function(sub.body))
    if any(_contains_caplog_access(n) for n in top_level):
        violations.extend(_scan_function(top_level))
    return sorted(set(violations))


def measured(root: Path = _ROOT) -> "tuple[dict[str, list[tuple[int, str]]], int]":
    """`(violations_by_file, scanned_file_count)` -- only files with at
    least one violation appear in the dict."""
    files = _iter_scan_files(root)
    by_file: "dict[str, list[tuple[int, str]]]" = {}
    for path in files:
        violations = find_violations(path)
        if violations:
            by_file[str(path.relative_to(root))] = violations
    return (by_file, len(files))


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options -- zero-tolerance gate, no baseline to write

    try:
        by_file, scanned = measured(_ROOT)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(
            f"unfiltered-caplog-consumption gate FAILED: could not scan the "
            f"population ({exc}). Fails CLOSED on a scan error rather than "
            f"silently under-counting.",
            file=sys.stderr,
        )
        return 1

    if scanned == 0:
        print(
            f"unfiltered-caplog-consumption gate FAILED: the scan found 0 "
            f"files under {_SCOPE}/ -- a scanner failure, not a clean "
            "population; `git ls-files` returned nothing.",
            file=sys.stderr,
        )
        return 1

    if not by_file:
        print(
            f"unfiltered-caplog-consumption gate OK: 0 unfiltered "
            f"caplog.records/.messages/.text consumption site(s) found "
            f"across {scanned} file(s) scanned under {_SCOPE}/."
        )
        return 0

    total = sum(len(v) for v in by_file.values())
    print("unfiltered-caplog-consumption gate FAILED:\n", file=sys.stderr)
    print(
        f"{total} unfiltered caplog.records/.messages/.text consumption "
        f"site(s) across {len(by_file)} file(s) -- the raw collection feeds "
        "an equality-with-literal comparison, a len() comparison, tuple/"
        "sequence unpacking, or an unfiltered any()/all() call, with no "
        "subject-specific (logger-name or message-content) filter applied "
        "first. Same class as #6031/#6035/#6038 -- under `-n auto`, an "
        "unrelated test's record on a different logger can flip this:",
        file=sys.stderr,
    )
    for file, violations in sorted(by_file.items()):
        for lineno, shape in violations:
            print(f"  {file}:{lineno}: {shape}", file=sys.stderr)
    print(
        "\nFilter to the subject before consuming: `r.name == "
        "\"your.logger.name\"` / `\"your message\" in r.message` in the "
        "comprehension's `if` clause (or the any()/all() predicate), "
        "before comparing/len()'ing/unpacking. This is a zero-tolerance "
        "gate -- no baseline, no --write-baseline escape hatch; every hit "
        "is a new regression.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
