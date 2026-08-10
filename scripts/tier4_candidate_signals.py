#!/usr/bin/env python3
"""#3878 Phase 2 mechanization — 5 candidate-surfacing signals for reading order.

## What this is, and what it deliberately is NOT

Phase 1/2 of #3878 hand-read a subset of `tests/` against the six questions
and found real Tier 4 tests, but the signals used to CHOOSE what to read next
(A/B/C in the issue's Phase 1, then "third-party import" in Phase 2) were all
ad-hoc human judgment, never captured in code — so the next person picking up
Phase 2 starts from zero again. This module makes the 5 candidate signals the
issue's own original plan named permanent, runnable functions.

**Enumeration, not judgment.** Every function here returns a LIST OF
CANDIDATES — files/tests that MATCH a structural pattern correlated with
Tier 4 in the small samples read so far. None of them decides Tier 4. A
human still reads each candidate and answers the six questions
(`docs/deep-dives/contributing/testing.md`) — this tool only decides what
order to read them in. **Not a CI gate** — nothing here has a pass/fail exit
code tied to a threshold; `main()` prints candidate counts and samples for a
human to triage, and always returns 0.

## The population-coverage premise (read before trusting a candidate count)

Phase 2's "5 consecutive rounds of 0 new Tier-4 found" applies to a
**62-file, 523-test subset** (files that directly import a third-party
library) — about 5.6% of `tests/`'s ~9,452 test functions. The remaining
~8,960 have never been sampled by ANY signal. A LOW candidate count from a
signal here is not evidence "the rest is clean" — it is only evidence about
what that specific signal catches, on the specific files it was run against.
Report counts as "N candidates from signal X, out of M functions scanned",
never as "the population is mostly Tier 1/2".

## The 5 signals (#3878's own original Phase 2 candidate list)

1. `third_party_only_asserts` — every assert in the test touches only a
   third-party/stdlib value, never anything the file imports from `reyn.*`.
2. `docstring_negative_with_issue` — the docstring names an issue number AND
   uses a negative framing ("not"/"never") — a common shape for a
   fingerprint-of-a-past-bug test (`docs/.../testing.md`'s Q1 discriminator).
3. `regression_named` — the test's own NAME contains `regression`, `not_`,
   or `no_` — the same fingerprint-naming smell, visible without reading the
   body at all.
4. `mass_produced_assert_shape` — N+ asserts in the SAME FILE share an
   identical structural shape (same AST after normalizing literal values) —
   a common symptom of the same check copy-pasted across many near-identical
   tests rather than genuinely distinct cases.
5. `narrow_tier2` — the docstring declares "Tier 2" but the test body calls
   into exactly ONE distinct `reyn.*`-sourced name — a broad Tier claim
   resting on a single, narrow call site.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"

# Signal 4's "mass produced" threshold — a knob, not a claim. 5+ identical
# assert shapes in one file is the starting point; adjust based on what a
# human triage pass finds useful, not a tuned-for-precision value.
_MASS_PRODUCED_THRESHOLD = 5


@dataclass
class Candidate:
    """One flagged test — file/name/line + which signal(s) flagged it."""

    path: Path
    test_name: str
    lineno: int
    signal: str
    detail: str = ""

    def __str__(self) -> str:
        rel = self.path.relative_to(_ROOT)
        base = f"{rel}:{self.lineno} {self.test_name} [{self.signal}]"
        return f"{base} — {self.detail}" if self.detail else base


def _iter_test_files(tests_dir: Path = _TESTS_DIR):
    for path in sorted(tests_dir.rglob("test_*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        yield path, tree


def _test_functions(tree: ast.AST):
    """Every top-level (not nested inside a class OR another function is
    fine — pytest collects both) test_* function/coroutine def."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node


def _reyn_bound_names(tree: ast.AST) -> "set[str]":
    """Every locally-bound name this file's imports give it that traces back
    to `reyn.*` — both `from reyn.x import Y` (binds `Y`) and `import reyn.x`
    (binds `reyn`, the root of the dotted access `reyn.x...`)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "reyn" or node.module.startswith("reyn.")
        ):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "reyn" or alias.name.startswith("reyn."):
                    names.add((alias.asname or alias.name.split(".")[0]))
    return names


def _dotted_base_name(node: ast.AST) -> "str | None":
    """For an Attribute chain (`a.b.c`), the root Name's id (`a`); None if
    the chain doesn't bottom out in a bare Name."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _references_any(node: ast.AST, names: "set[str]") -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in names:
            return True
        if isinstance(n, ast.Attribute):
            base = _dotted_base_name(n)
            if base in names:
                return True
    return False


# ── signal 1 ─────────────────────────────────────────────────────────────


def third_party_only_asserts(tests_dir: Path = _TESTS_DIR) -> "list[Candidate]":
    """Every assert in the test touches only third-party/stdlib values,
    never anything the file imports from `reyn.*` — the Q1 "whose bug is
    it" discriminator, made mechanical: if NOTHING in the test's own
    assertions ever names reyn's own code, a failure here is unlikely to be
    reyn's bug."""
    out: list[Candidate] = []
    for path, tree in _iter_test_files(tests_dir):
        reyn_names = _reyn_bound_names(tree)
        if not reyn_names:
            continue  # a file with no reyn import at all is out of scope for this signal
        for fn in _test_functions(tree):
            asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
            if not asserts:
                continue
            if all(not _references_any(a.test, reyn_names) for a in asserts):
                out.append(Candidate(
                    path, fn.name, fn.lineno, "third_party_only_asserts",
                    f"{len(asserts)} assert(s), none reference reyn.*",
                ))
    return out


# ── signal 2 ─────────────────────────────────────────────────────────────

_ISSUE_NUM_RE = re.compile(r"#\d+")
_NEGATIVE_RE = re.compile(r"\b(not|never)\b", re.IGNORECASE)


def docstring_negative_with_issue(tests_dir: Path = _TESTS_DIR) -> "list[Candidate]":
    """The docstring names an issue number AND uses a negative framing
    ("not"/"never") — a common shape for a past-bug fingerprint test
    (`assert "X" not in result`, which any OTHER wrong value also passes)."""
    out: list[Candidate] = []
    for path, tree in _iter_test_files(tests_dir):
        for fn in _test_functions(tree):
            doc = ast.get_docstring(fn) or ""
            if _ISSUE_NUM_RE.search(doc) and _NEGATIVE_RE.search(doc):
                out.append(Candidate(
                    path, fn.name, fn.lineno, "docstring_negative_with_issue",
                    doc.splitlines()[0][:100] if doc else "",
                ))
    return out


# ── signal 3 ─────────────────────────────────────────────────────────────

_NAME_SMELL_RE = re.compile(r"regression|(?:^|_)not_|(?:^|_)no_")


def regression_named(tests_dir: Path = _TESTS_DIR) -> "list[Candidate]":
    """The test's own NAME contains `regression`, `not_`, or `no_` — a
    fingerprint-naming smell visible without reading the body."""
    out: list[Candidate] = []
    for path, tree in _iter_test_files(tests_dir):
        for fn in _test_functions(tree):
            if _NAME_SMELL_RE.search(fn.name):
                out.append(Candidate(path, fn.name, fn.lineno, "regression_named"))
    return out


# ── signal 4 ─────────────────────────────────────────────────────────────


def _normalized_assert_shape(node: ast.Assert) -> str:
    """ast.dump of the assert's test expression with every Constant value
    replaced by a placeholder — so `assert x == 1` and `assert x == 2` dump
    identically (same SHAPE, different literal) while `assert x == 1` and
    `assert y == 1` do not (different variable)."""

    class _Blank(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant) -> ast.Constant:
            return ast.copy_location(ast.Constant(value="<LIT>"), node)

    blanked = _Blank().visit(ast.parse(ast.unparse(node.test), mode="eval"))
    return ast.dump(blanked, annotate_fields=False)


def mass_produced_assert_shape(
    tests_dir: Path = _TESTS_DIR, threshold: int = _MASS_PRODUCED_THRESHOLD
) -> "list[Candidate]":
    """N+ asserts in the SAME FILE share an identical structural shape
    (same AST after normalizing literal values) — a common symptom of the
    same check copy-pasted across many near-identical tests."""
    out: list[Candidate] = []
    for path, tree in _iter_test_files(tests_dir):
        shape_to_asserts: "defaultdict[str, list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Assert]]]" = defaultdict(list)
        for fn in _test_functions(tree):
            for node in ast.walk(fn):
                if isinstance(node, ast.Assert):
                    try:
                        shape = _normalized_assert_shape(node)
                    except (SyntaxError, ValueError):
                        continue
                    shape_to_asserts[shape].append((fn, node))
        for shape, hits in shape_to_asserts.items():
            if len(hits) < threshold:
                continue
            fns_involved = {fn.name for fn, _ in hits}
            for fn, node in hits:
                out.append(Candidate(
                    path, fn.name, node.lineno, "mass_produced_assert_shape",
                    f"{len(hits)}x identical assert shape across {len(fns_involved)} test(s) in this file",
                ))
    return out


# ── signal 5 ─────────────────────────────────────────────────────────────

_TIER2_RE = re.compile(r"^Tier 2\b")


def narrow_tier2(tests_dir: Path = _TESTS_DIR) -> "list[Candidate]":
    """The docstring declares "Tier 2" but the test body calls into exactly
    ONE distinct reyn.*-sourced name — a broad Tier claim resting on a
    single, narrow call site."""
    out: list[Candidate] = []
    for path, tree in _iter_test_files(tests_dir):
        reyn_names = _reyn_bound_names(tree)
        if not reyn_names:
            continue
        for fn in _test_functions(tree):
            doc = ast.get_docstring(fn) or ""
            if not _TIER2_RE.match(doc.strip()):
                continue
            touched: set[str] = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and n.id in reyn_names:
                    touched.add(n.id)
                elif isinstance(n, ast.Attribute):
                    base = _dotted_base_name(n)
                    if base in reyn_names:
                        touched.add(f"{base}.{n.attr}")
            if len(touched) == 1:
                out.append(Candidate(
                    path, fn.name, fn.lineno, "narrow_tier2",
                    f"only touches {next(iter(touched))}",
                ))
    return out


ALL_SIGNALS = {
    "third_party_only_asserts": third_party_only_asserts,
    "docstring_negative_with_issue": docstring_negative_with_issue,
    "regression_named": regression_named,
    "mass_produced_assert_shape": mass_produced_assert_shape,
    "narrow_tier2": narrow_tier2,
}


def _total_test_functions(tests_dir: Path = _TESTS_DIR) -> int:
    total = 0
    for _, tree in _iter_test_files(tests_dir):
        total += sum(1 for _ in _test_functions(tree))
    return total


def main(argv: "list[str] | None" = None) -> int:
    """Print candidate counts per signal + a sample, against the full
    scanned population size — never a pass/fail verdict. Always returns 0:
    this is a reading-order tool, not a gate (#3878 explicit instruction)."""
    del argv
    total = _total_test_functions()
    print(f"Scanned {total} test functions under {_TESTS_DIR.relative_to(_ROOT)}.\n")
    for name, fn in ALL_SIGNALS.items():
        candidates = fn()
        pct = (len(candidates) / total * 100) if total else 0.0
        print(f"[{name}] {len(candidates)} candidate(s) ({pct:.1f}% of {total} scanned — NOT a population estimate)")
        for c in candidates[:10]:
            print(f"  {c}")
        if len(candidates) > 10:
            print(f"  ... and {len(candidates) - 10} more")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
