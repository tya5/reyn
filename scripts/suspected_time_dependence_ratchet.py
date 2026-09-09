#!/usr/bin/env python3
"""#4846 — a ratchet over SUSPECTED time-dependent test shapes, not a violation count.

## Why "suspected", never "violations" (architect ruling, #4846)

CLAUDE.md's "A test writes no duration" is actually two separate rules, and
only one of them is checkable by syntax alone:

- **Ceiling** (how long CI will wait) — `@pytest.mark.timeout(N)` and a
  `for _ in range(N):` loop wrapping an `await`/`sleep`/`pause` are both
  pure SYNTAX: the shape itself is the violation, nothing about the
  test's semantics needs to be known. **Gate-able, low false-positive.**
- **Floor** (how long something must take) — "a test writes a `sleep(N)`
  duration ITS OWN ASSERT DEPENDS ON" needs the dependency from the sleep
  to the assert, which is a SEMANTIC question no AST walk answers. A
  `time.sleep(0.05)` letting a background thread settle before an
  unrelated assert is fine; the identical syntax gating a race is the
  violation this rule actually names — and this script cannot tell them
  apart.

A 2026-08-15 whole-`tests/` AST census (the design brief this ratchet
implements) found 347 syntactically-suspicious sites and explicitly
could NOT classify how many are real floor violations — "my classifier
judges by AST shape alone, it never looked at meaning; the ones marked
harmless could still have a real wait inside them, unmeasured." That
gap is why this script's name, its baseline filename, and every line of
its output say **suspected**, never **violation** or **offender** — a
count of confirmed-real duration-dependence does not exist, and this
ratchet does not manufacture one by naming itself as though it did.

## Two SEPARATE ratchets, not one number

The ceiling population and the floor population are counted independently,
per file, in two separate sections of the same baseline. Folding them into
one number would let a syntactically-certain ceiling violation hide inside
the same total as an unconfirmed floor suspicion — and would remove the one
place a future reader could put a hard "this must reach zero" guarantee:
`@pytest.mark.timeout` can be *proven*, by grep alone, to be fully removable
(#4844 already did exactly that for one file) — the ceiling side is the
one where "baseline reaches empty" is a real, checkable goal. The floor
side, being semantic, has no such promise attached; it only ratchets
against GROWTH, the same as `mypy_ratchet.py` does for a population nobody
claims is fully triaged.

## What this catches (three AST shapes, `ast`, never `grep`)

1. **Floor — `time.sleep(x)` / `asyncio.sleep(x)`, `x` not provably `0`.**
   `sleep(0)`/`sleep(0.0)` (a cooperative yield, not a wait) is explicitly
   NOT counted — the one place this script positively clears a shape
   rather than merely failing to flag it.
2. **Ceiling — `for _ in range(N): ...`** whose body contains an
   `await` expression, or a call whose name/attribute ends in `sleep` or
   equals `pause` — "attempts=200"-style polling wrapped in a bounded
   retry count.
3. **Ceiling — `@pytest.mark.timeout(...)`** on any function.

## What this does NOT catch (disclosed, not hidden — architect's own 2 additions)

- **Any rebinding or indirection.** `sleep = time.sleep` then `sleep(5)`;
  `functools.partial(time.sleep, 5)`; a project helper that wraps `sleep`
  internally — all invisible to a plain AST walk matching on the call's
  literal name. A false sense of safety here is worse than a disclosed
  gap; this script does not attempt name resolution across imports/
  bindings, and undercounts as a direct result.
- **Time-dependence with no `sleep`, no `range`, no marker at all.**
  #5918 (2026-09-07) is the concrete instance: a test drives a real call
  with `timeout=5.0` and its assert depends on how many retries land
  inside that window — no `sleep()` call, no `range()` loop, no
  `@pytest.mark.timeout` anywhere in the test. This ratchet's three
  detectors do not fire on that shape and never will without becoming a
  semantic (not syntactic) check — which is exactly the "floor" half
  architect ruled ungateable. **A green run of this script is not
  evidence a test's assert is duration-independent** — it is evidence
  only that these three specific syntax shapes did not grow. That
  distinction is reviewed at the six-questions/co-vet layer, not here.

## No exception-comment escape hatch

There is no `# noqa`-shaped marker this script honors, deliberately
(architect + lead-coder, both explicit on this point). A legitimate new
`sleep()` (building a real stall for a stall-detector test, say) is
declared the same way every other addition to this ratchet is: run
`--write-baseline` and say why in the PR body. A baseline bump shows up
in the diff and gets reviewed; a `# noqa`-style comment does not.

## Ratchet mechanics (same skeleton as `mypy_ratchet.py` / `flat_tests_ratchet.py`)

Baseline is a per-file COUNT, not a set of names — `{"ceiling": {file:
count}, "floor": {file: count}}`. A file's count going UP in either
section, in a file already in the baseline, is new debt and fails. A
file appearing with a nonzero count that isn't in the baseline at all is
new debt and fails. A file's count going DOWN — or a file dropping out
entirely — silently tightens the effective floor the next
`--write-baseline` run will commit; nothing has to be edited to let a
fix "count."

## Vacuity guard (architect's own acceptance criterion)

The ratchet's own pass/fail comparison only ever proves "did the count
change" — it says nothing about whether the SCAN itself ran at all. A
scanner that silently found zero `.py` files under `tests/` (a moved
root, a broken `git ls-files` invocation, a bug in this script) would
read as a healthy, fully-baselined zero, indistinguishable from "347
suspects, all now fixed." `main()` asserts the scanned-file count is
nonzero BEFORE ever comparing against the baseline, and fails loud,
separately from the ratchet's own pass/fail, if it is not.

CI: gate
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "suspected_time_dependence_ratchet_baseline.json"

_SLEEP_NAMES = frozenset({"sleep"})
_PAUSE_NAMES = frozenset({"pause"})


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked `tests/**/*.py` file — `git ls-files`, the same
    population source `check_tests_path_literal_reference.py` uses and for
    the same reason: it already excludes `.venv/`, `__pycache__/`, and
    anything gitignored, with no hand-maintained exclusion list to keep in
    sync."""
    proc = subprocess.run(
        ["git", "ls-files", "--", "tests/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def _is_provably_zero(node: ast.expr) -> bool:
    """True only when *node* is the literal `0` or `0.0` — a cooperative
    yield, not a wait. Anything else (a name, an expression, a negative
    literal, a call) is NOT provably zero and counts as suspected; this
    function only ever clears a shape, never flags one."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and node.value == 0
    )


def _call_name(node: ast.expr) -> str:
    """The trailing name of a call target — `sleep` from both `time.sleep`
    and a bare `sleep`, `pause` from `some.mod.pause`. Deliberately name-
    only, no import/binding resolution (see module docstring's disclosed
    gap: `sleep = time.sleep` is invisible to this)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _contains_wait(body: "list[ast.stmt]") -> bool:
    """True if any statement in *body* (walked recursively) is an `await`
    expression, or a call whose name ends in `sleep`/equals `pause` — the
    "waits inside a bounded retry loop" shape (design brief's `range(N)`
    population)."""
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Await):
                return True
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name in _SLEEP_NAMES or name in _PAUSE_NAMES:
                    return True
    return False


def _is_range_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _call_name(node.func) == "range"


def suspected_counts(path: Path) -> "tuple[int, int]":
    """`(ceiling_count, floor_count)` for one file — every top-level and
    nested node walked once via `ast.walk`, so a suspected shape inside a
    helper function, a nested `for`, or a decorated coroutine is still
    counted."""
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        # A file this ratchet cannot parse contributes nothing to either
        # count — not the same as "scanned and found clean" at the WHOLE-
        # RUN level (see the vacuity guard: that is checked against the
        # file COUNT, not against any one file's own parse success).
        return (0, 0)

    ceiling = 0
    floor = 0

    for node in ast.walk(tree):
        # Ceiling ①: @pytest.mark.timeout(...) on any function.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "timeout"
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "mark"
                ):
                    ceiling += 1

        # Ceiling ②: for _ in range(N): <body with an await/sleep/pause>.
        if isinstance(node, ast.For) and _is_range_call(node.iter):
            if _contains_wait(node.body):
                ceiling += 1

        # Floor: time.sleep(x) / asyncio.sleep(x), x not provably 0.
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _SLEEP_NAMES and node.args and not _is_provably_zero(node.args[0]):
                floor += 1

    return (ceiling, floor)


def measured(root: Path = _ROOT) -> "tuple[dict[str, int], dict[str, int], int]":
    """`(ceiling_by_file, floor_by_file, scanned_file_count)` — only files
    with a nonzero count appear in either dict (matching the other ratchets'
    own "absence means zero" convention); `scanned_file_count` is the total
    number of files the scan actually walked, used ONLY for the vacuity
    guard, never for the pass/fail comparison itself."""
    files = _iter_scan_files(root)
    ceiling_by_file: "dict[str, int]" = {}
    floor_by_file: "dict[str, int]" = {}
    for path in files:
        ceiling, floor = suspected_counts(path)
        rel = str(path.relative_to(root))
        if ceiling:
            ceiling_by_file[rel] = ceiling
        if floor:
            floor_by_file[rel] = floor
    return (ceiling_by_file, floor_by_file, len(files))


def load_baseline(path: Path = _BASELINE_PATH) -> "tuple[dict[str, int], dict[str, int]]":
    data = json.loads(path.read_text(encoding="utf-8"))
    return (data.get("ceiling", {}), data.get("floor", {}))


def write_baseline(
    ceiling: "dict[str, int]", floor: "dict[str, int]", path: Path = _BASELINE_PATH,
) -> None:
    data = {
        "ceiling": dict(sorted(ceiling.items())),
        "floor": dict(sorted(floor.items())),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def grown_files(measured_by_file: "dict[str, int]", baseline_by_file: "dict[str, int]") -> "dict[str, tuple[int, int]]":
    """`{file: (baseline_count, measured_count)}` for every file whose
    measured count exceeds its baseline count — a file absent from the
    baseline is compared against 0."""
    grown: "dict[str, tuple[int, int]]" = {}
    for file, count in measured_by_file.items():
        base = baseline_by_file.get(file, 0)
        if count > base:
            grown[file] = (base, count)
    return grown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from the CURRENT suspected counts "
            "instead of checking against them. Use for initial adoption, a "
            "real reduction you want to lock in, or a deliberate, reviewed "
            "new sleep/range/timeout — say why in the PR body. There is no "
            "other way to silence a new suspected site (see module "
            "docstring: no exception-comment escape hatch)."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    ceiling, floor, scanned = measured(_ROOT)

    # Vacuity guard, checked BEFORE anything else and independent of
    # --write-baseline: a scan that found zero files is a scanner failure,
    # not a clean population, and must never read as "OK" either way.
    if scanned == 0:
        print(
            "suspected-time-dependence ratchet FAILED: the scan found 0 "
            "files under tests/ — this is a scanner failure (a moved root, "
            "a broken `git ls-files` invocation, a bug in this script), not "
            "a clean population. A baseline comparison against zero would "
            "read as healthy regardless of which; refusing to make that "
            "claim.",
            file=sys.stderr,
        )
        return 1

    if args.write_baseline:
        # Pass `_BASELINE_PATH` explicitly, by name, rather than relying on
        # the callee's own default parameter value — a default is bound
        # once at function-DEFINITION time, so a test that monkeypatches
        # this module's `_BASELINE_PATH` would silently write to the REAL
        # path instead (flat_tests_ratchet.py's own main() carries the same
        # comment, for the same reason).
        write_baseline(ceiling, floor, _BASELINE_PATH)
        print(
            f"Wrote suspected counts for {len(ceiling)} file(s) (ceiling) / "
            f"{len(floor)} file(s) (floor) to {_BASELINE_PATH}, "
            f"{scanned} file(s) scanned."
        )
        return 0

    base_ceiling, base_floor = load_baseline(_BASELINE_PATH)
    ceiling_grown = grown_files(ceiling, base_ceiling)
    floor_grown = grown_files(floor, base_floor)

    if not ceiling_grown and not floor_grown:
        print(
            "suspected-time-dependence ratchet OK: "
            f"{sum(ceiling.values())} suspected ceiling site(s) / "
            f"{sum(floor.values())} suspected floor site(s), all baselined "
            f"({scanned} file(s) scanned). Suspected, not confirmed — see "
            "module docstring for what this does and does not catch."
        )
        return 0

    print("suspected-time-dependence ratchet FAILED:\n", file=sys.stderr)
    if ceiling_grown:
        print(
            f"{len(ceiling_grown)} file(s) grew their SUSPECTED CEILING count "
            "(@pytest.mark.timeout / a range(N) loop wrapping a wait) versus "
            f"the baseline ({_BASELINE_PATH.relative_to(_ROOT)}):",
            file=sys.stderr,
        )
        for file, (base, count) in sorted(ceiling_grown.items()):
            print(f"  {file}: {base} -> {count}", file=sys.stderr)
    if floor_grown:
        print(
            f"{len(floor_grown)} file(s) grew their SUSPECTED FLOOR count "
            "(sleep(x), x not provably 0) versus the baseline:",
            file=sys.stderr,
        )
        for file, (base, count) in sorted(floor_grown.items()):
            print(f"  {file}: {base} -> {count}", file=sys.stderr)
    print(
        "\nThese are SUSPECTED sites, not confirmed violations — this "
        "script judges syntax, never whether an assert actually depends on "
        "the wait. If the new site is a genuine, reviewed addition (a real "
        "stall for a stall-detector test, a legitimate settle-window), say "
        "so in the PR body and run --write-baseline. There is no comment-"
        "based exception to this gate.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
