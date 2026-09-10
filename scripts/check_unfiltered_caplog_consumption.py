#!/usr/bin/env python3
"""Recurrence-prevention gate for the caplog-consumption bug pattern that hit
#6031, #6035, and #6038 independently, the same day (2026-09-10): a test
asserts something about `caplog.records`/`caplog.messages`/`caplog.text`
WITHOUT filtering to the test's own subject (logger name or message
content) first. `caplog`'s handler is attached to the ROOT logger for the
whole pytest-xdist worker process, not scoped to one test -- under `-n
auto`, an unrelated test's background record on a DIFFERENT logger (a
leaked async client's finalizer warning, a lingering timer) can land inside
the window and flip an unfiltered assertion. This is flaky-by-construction,
never a real invariant about the subject under test.

## Classification axis: ALLOWLIST the safe shapes, not denylist the dangerous ones

Per lead-coder's correction on the PR that introduced this gate
(https://github.com/tya5/reyn/pull/6040#issuecomment-5611043646): an earlier
revision of this classifier enumerated three "dangerous shapes" (equality,
`len()`, unpack) as the things it checked for. That is an enumeration of the
author's imagination, not of the population -- and it missed a 4th live
shape the very same measurement disclosed as "out of scope" (bare
truthiness, `assert not caplog.records`), which is the SAME hazard class
under a different spelling. Enumerating danger always has this failure mode:
a 5th, as-yet-unseen shape is missed the same way the 4th was.

The classifier therefore asks the opposite question. A consumption of
`caplog.records`/`.messages`/`.text` (or a name it was assigned to, untouched)
is SAFE if and only if it is one of exactly two shapes:

1. a **membership/containment check** -- `in`/`not in` against a literal or
   a variable holding subject-specific text, anywhere inside a boolean
   expression (including as the sole predicate to `any()`/`all()`). The
   containment check itself IS the subject-specific filter, by construction
   (`"foo" in caplog.text`).
2. a **filtered comprehension/generator** -- `[r for r in caplog.records if
   r.name == X]`, or the equivalent `any(r.name == X for r in
   caplog.records)` shape where the generator's element/predicate is itself
   subject-specific (a record's `.name` or `.message`/`.getMessage()`, or a
   nested `in`/`not in` literal check).

**Every other consumption is dangerous** -- comparison (equality, `len()`
feeds into it structurally so `len()` is flagged directly rather than
waiting for the comparison around it; any non-containment comparison
operator), tuple/sequence unpacking, boolean/truthiness (`if
caplog.records:`, `assert not caplog.records`, `bool(caplog.records)`,
a `BoolOp` operand), indexing (`caplog.records[0]`), and bare iteration
over the raw collection outside a comprehension (`for r in caplog.records:
...`) with no subject-specific filter applied first. The implementation
below is structured as "is this one of the two safe shapes? if not, flag
it" -- each consumption *context* (Compare, Call, Assign-unpack, boolean
test, Subscript, bare `for`) is checked against the allowlist, and anything
that reaches that context without matching it is flagged by the SAME
fallthrough, not by a separate per-shape rule. A 5th/6th shape this gate's
author has not yet imagined, expressed through one of these contexts, is
caught automatically because the default for every context is "dangerous",
not "ignore".

## Read-only measurement that justified building this (origin PR, #6040)

A hand-written AST walk over every `caplog.records`/`.messages`/`.text`
consumption site in `tests/**/*.py` found 112 total sites under the
ORIGINAL (enumerated-dangerous) classifier: 108 already subject-specific
and 4 genuinely unfiltered -- the 3 incidents above (each independently
fixed in its own PR before this gate existed) plus 2 that were still live
at measurement time, fixed in the gate's own PR. The later inversion (this
revision) re-ran against the same population and additionally caught the
3 bare-truthiness sites disclosed-but-not-fixed in that PR
(`tests/hooks/test_hook_shell_push_2069.py:205`, `tests/runtime/
test_5982_internal_boundary_fold.py:190`, `tests/security/
test_sandbox_capability_declaration_4935.py:205`) -- fixed in the SAME PR
that landed this inversion. See that PR's follow-up commit for the full
re-verification count against the current tree.

## What this gate does NOT attempt (disclosed, not solved)

This is a **syntax-shape** classifier (`ast.walk`, no dataflow, no type
inference), same posture `suspected_time_dependence_ratchet.py` (#4846)
and `silent_except_ratchet.py` (#5990) already established for this repo's
AST gates -- "suspected", not "confirmed".

- Variable-derivation tracing is single-hop (`x = <expr>`, then `x` used
  in a dangerous context) and does not follow a value through more than
  one reassignment, a function call, or a non-`Name` target. A pattern
  that launders an unfiltered collection through an intermediate helper
  function before consuming it would not be caught -- the classifier has
  no opinion on a caplog-derived value passed as an argument to an
  arbitrary function call (neither `len`/`any`/`all`/`bool`), which is the
  one context this allowlist-by-context approach cannot close without
  dataflow analysis.
- A bare `for` loop whose BODY filters by subject before acting (`for r in
  caplog.records:\n    if r.name == X: ...`) is still flagged as "bare
  iteration" -- the two sanctioned safe shapes are containment and a
  filtered comprehension/generator specifically, not an arbitrarily-shaped
  loop body. No real site in this population uses that shape today; a
  future one would need to be rewritten as a filtered comprehension rather
  than grandfathered in.

## `caplog.text` scope decision (explicit, per lead-coder's requirement)

`.text`, `.records`, and `.messages` are classified identically by the same
allowlist rule above -- no special-casing. A future dangerous `.text` site
is already covered by the same rule that covers `.records`/`.messages`
today; nothing here needs updating first.

## False-positive / recall disclosure (condition 4, explicit)

False-positive rate is measured at ~0 for the current tree: run against
the real `tests/**/*.py` population, the inverted classifier flags 0 sites
beyond the 8 known dangerous ones (5 fixed in #6040's first revision, 3
bare-truthiness sites fixed in this revision) -- see the PR's re-
verification comment for the exact count and revision SHA. Recall is NOT
claimed complete -- it is verified only against these 8 known incident
shapes, all of which it correctly flags before their fix and clears after.
Whether it would catch every FUTURE unfiltered-caplog shape is unmeasured
and not claimed; see the disclosed gaps above for the two shapes it
structurally cannot close without dataflow analysis.

## No baseline / grandfather list (explicit, per lead-coder's constraint)

This is a zero-tolerance gate, not a ratchet: no baseline JSON, no
`--write-baseline` escape hatch. The tree is clean today -- any hit is a
NEW regression, not inherited debt to be silenced.

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

# This gate's OWN fixture files deliberately contain every dangerous shape
# it must catch (`dangerous.py`) -- scanning them as part of the
# zero-tolerance real-tree population would make
# `test_the_real_repo_tree_is_currently_clean` permanently red for a
# reason that is not a regression. The one-line exclusion is the gate's
# self-reference, not a hand-maintained denylist of real sites.
_EXCLUDED_PREFIX = "tests/scripts/fixtures/unfiltered_caplog_6042/"


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked `.py` file under `tests/` -- `git ls-files`, the same
    population source `silent_except_ratchet.py`/`check_no_core_dep_
    importorskip.py` use, for the same reason: no hand-maintained
    exclusion list, and it already excludes anything gitignored -- except
    this gate's own fixture directory (see `_EXCLUDED_PREFIX`)."""
    proc = subprocess.run(
        ["git", "ls-files", "--", f"{_SCOPE}/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [
        root / line
        for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith(_EXCLUDED_PREFIX)
    ]


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
    """For *value* (a caplog-related expression), return True if it is
    already subject-filtered (SAFE -- one of the two allowlisted shapes),
    False if it is raw/unfiltered (DANGEROUS the moment it reaches a
    consuming context), or None if *value* has nothing to do with caplog
    at all (not this gate's concern)."""
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
    handled separately, as a dangerous CONTEXT in their own right (see
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
    derivations are recorded as they're seen, then every statement is also
    checked for a direct consumption CONTEXT. Each `visit_*` method below
    corresponds to a *context* a caplog-derived value can be consumed
    through (comparison, call, unpack-assignment, boolean test, subscript,
    bare iteration) -- not to a named "dangerous shape". Inside each, the
    only way to come out SAFE is to match one of the two allowlisted
    shapes (containment via `in`/`not in`, or a filtered comprehension/
    generator already folded into `_classify_value_expr`'s True verdict);
    everything else that reaches the context falls through to DANGEROUS.
    A consumption context this file has no `visit_*` for is undetected --
    the single-hop/no-dataflow disclosure in the module docstring, not a
    silent allow."""
    analysis = _FunctionAnalysis()
    violations: "list[tuple[int, str]]" = []

    def _is_raw_unfiltered(node: "ast.AST | None") -> bool:
        return _classify_value_expr(node, analysis) is False if node is not None else False

    class _Visitor(ast.NodeVisitor):
        def visit_Assign(self, node: ast.Assign) -> None:
            # Unpacking target off a caplog-derived value: no allowlisted
            # shape exists for this context at all -- always dangerous.
            if any(isinstance(t, (ast.Tuple, ast.List)) for t in node.targets):
                if _is_raw_unfiltered(node.value):
                    violations.append((node.lineno, "tuple/sequence unpacking"))
            else:
                _record_derivation(node, analysis)
            self.generic_visit(node)

        def visit_Compare(self, node: ast.Compare) -> None:
            # Allowlist: an `in`/`not in` op touching the value IS the
            # subject-specific check (shape 1). Every other comparison
            # operator (Eq, NotEq, Lt, Gt, ...) falls through to dangerous.
            values = [node.left, *node.comparators]
            for idx, v in enumerate(values):
                if not _is_raw_unfiltered(v):
                    continue
                if _compare_touches_op(node, idx, (ast.In, ast.NotIn)):
                    continue  # allowlisted: containment IS the filter
                violations.append((node.lineno, "comparison"))
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            fname = func.id if isinstance(func, ast.Name) else None
            if fname == "len":
                for arg in node.args:
                    if _is_raw_unfiltered(arg):
                        violations.append((node.lineno, "len() comparison"))
            elif fname == "bool":
                for arg in node.args:
                    if _is_raw_unfiltered(arg):
                        violations.append((node.lineno, "truthiness"))
            elif (
                fname in ("any", "all")
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
                        violations.append((node.lineno, f"unfiltered {fname}()"))
            self.generic_visit(node)

        def _flag_truthiness(self, test: "ast.AST | None") -> None:
            if test is not None and _is_raw_unfiltered(test):
                violations.append((test.lineno, "truthiness"))

        def visit_If(self, node: ast.If) -> None:
            self._flag_truthiness(node.test)
            self.generic_visit(node)

        def visit_While(self, node: ast.While) -> None:
            self._flag_truthiness(node.test)
            self.generic_visit(node)

        def visit_IfExp(self, node: ast.IfExp) -> None:
            self._flag_truthiness(node.test)
            self.generic_visit(node)

        def visit_Assert(self, node: ast.Assert) -> None:
            self._flag_truthiness(node.test)
            self.generic_visit(node)

        def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
            if isinstance(node.op, ast.Not):
                self._flag_truthiness(node.operand)
            self.generic_visit(node)

        def visit_BoolOp(self, node: ast.BoolOp) -> None:
            for v in node.values:
                self._flag_truthiness(v)
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            if _is_raw_unfiltered(node.value):
                violations.append((node.lineno, "indexing"))
            self.generic_visit(node)

        def visit_For(self, node: ast.For) -> None:
            # A comprehension's own `for` clause is an `ast.comprehension`,
            # a different node type -- this is only a genuine bare `for`
            # statement, which has no allowlisted shape of its own.
            if _is_raw_unfiltered(node.iter):
                violations.append((node.lineno, "bare iteration"))
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
        f"site(s) across {len(by_file)} file(s) -- the raw collection (or a "
        "name derived from it) reaches a comparison, len(), tuple/sequence "
        "unpacking, a boolean/truthiness test, indexing, or bare iteration "
        "with no subject-specific (logger-name or message-content) filter "
        "applied first. Same class as #6031/#6035/#6038 -- under `-n "
        "auto`, an unrelated test's record on a different logger can flip "
        "this:",
        file=sys.stderr,
    )
    for file, violations in sorted(by_file.items()):
        for lineno, shape in violations:
            print(f"  {file}:{lineno}: {shape}", file=sys.stderr)
    print(
        "\nFilter to the subject before consuming: `r.name == "
        "\"your.logger.name\"` / `\"your message\" in r.message` in the "
        "comprehension's `if` clause (or the any()/all() predicate), or as "
        "an `in`/`not in` containment check, before comparing/len()'ing/"
        "unpacking/truthiness-testing/indexing/iterating. This is a "
        "zero-tolerance gate -- no baseline, no --write-baseline escape "
        "hatch; every hit is a new regression.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
