#!/usr/bin/env python3
"""#4065 — every `tests/...py` path literal, repo-wide, must resolve to a real file.

## The class this closes

A path literal referencing a `tests/` file — in a docstring, a code comment,
a doc's prose, a YAML/workflow file's command args — goes stale the moment
that file moves, UNLESS something checks it. #3879's bucket migrations broke
this 3 times, each caught by a DIFFERENT, weaker mechanism:

  - #4025: a stale assert inside `tests/builtin/` — caught because CI ran it
    and it went RED.
  - #4036: a stale `pytest <path>` arg inside a `.github/workflows/*.yml`
    file — caught because pytest happened to exit 4 (0 collected) rather
    than silently passing. Coincidental, not a mechanism.
  - #4062/#4063: stale prose in `docs/`/`src/`/`scripts/` — caught only by
    a human running `grep` by hand. **Nothing executes this text, so
    nothing was ever going to turn red on its own.**

The third shape is the dangerous one: it stays green forever, because
nothing exercises it. This script is the missing "does it still exist"
check for exactly that shape — a population scan, not an execution path.

## Two mistakes this design deliberately avoids (both made twice during #3879)

1. **Scoping the population to `tests/` alone.** The reference and the
   referenced file are on OPPOSITE sides — the reference lives in `docs/`,
   `src/`, `scripts/`, `.github/`, anywhere in the repo, while only the
   referenced FILE lives under `tests/`. Scoping the SCAN to `tests/` (the
   #3879 bucket-migration PRs' own repeated mistake) misses every one of
   these by construction. This script scans the WHOLE tracked repository.
2. **Requiring the match to start right after an opening quote.** #4006's
   own measured lesson: anchoring a `tests/` literal detector to "the quote
   character is immediately followed by `tests/`" undercounted a real
   population by 17x (17 found vs. 287 actual) — a huge fraction of real
   references are embedded mid-sentence ("the gate in a bucket subdirectory
   fires", "RED at the failing test's own module path"), not standalone
   path strings — deliberately not spelled out as a literal tests/-shaped
   example here, so this docstring's own prose doesn't become a hit
   against this gate's own scan (same reasoning below, and in the paired
   test file's `_T` split).
   This script's regex matches `tests/...` ANYWHERE
   inside a larger string/prose run, with a word boundary on the left, not
   only at a string literal's own start.

## Scope: text content, not just Python source

Docstrings, code comments, plain string literals, Markdown prose, YAML
values, workflow files — anywhere a person or tool might type a path. The
scan is a plain regex over raw file text, not an AST walk restricted to
`.py` files — the whole point is to catch prose in `.md`/`.yml` too, which
an AST-based scan structurally cannot see.

## A ratchet, not a zero baseline (#4065's own design pivot)

A real, whole-repo measurement returns several hundred hits, most of them
pre-existing debt (a reference that WAS real and went stale when its file
moved, long before this gate existed — not something a #3879-era PR
introduced). Requiring that backlog closed before adoption defers adoption
indefinitely, so — same skeleton as ``mypy_ratchet.py`` — this is a
committed BASELINE of every ``(referencing_file, literal)`` pair the tree
currently carries. A pair not in the baseline is new — CI fails immediately.
A pair that WAS in the baseline and is gone (because someone fixed it, or
the referencing file itself moved/was deleted) just silently stops
appearing; nothing has to be edited to let a fix "count."

Deliberately keyed on ``(referencing_file, literal)``, not on the line
number: an unrelated edit shifting a line above a reference must never
itself flip the gate red, the same reasoning ``mypy_ratchet.py`` gives for
excluding its own error line number from the key.

**No placeholder-name allowlist.** A hand-picked list of "illustrative
example" basenames (``test_a.py``, ``test_x.py``, ...) was considered and
rejected: it would silently swallow every FUTURE reference sharing one of
those names too, including a genuine new stale reference, defeating the one
thing a ratchet is for — surfacing every new addition for a human to triage.
The baseline already grandfathers every current illustrative-example hit
(measured, not guessed) exactly like any other pre-existing entry; nothing
about "is this one illustrative" needs to be decided up front.

## Verified baseline

## Population: `git ls-files`, not a directory walk

The scan population is TRACKED files (`git ls-files`), not `Path.rglob()`
filtered by a hand-maintained excluded-directories list. `git ls-files`
already answers "is this real repo content" with zero rules of its own to
keep in sync — `site/` (mkdocs' build output), `.venv/`, `__pycache__/`,
node_modules, every build/VCS-internal directory, are simply never in its
output, because none of them are tracked. A future build tool that dumps
its own generated tree somewhere new is excluded automatically, for free,
the same day it starts being gitignored — no second exclusion list to
remember to extend (lead-coder review, #4065: a directory-exclusion list
for `site/` would have needed re-deriving by hand for every future
generated-output directory; `git ls-files` needs none).

Run directly against the real repo tree; see ``check_tests_path_literal_reference_baseline.json``
and the paired test file's ``test_the_real_repo_tree_measurement_matches_the_baseline``.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "check_tests_path_literal_reference_baseline.json"

# A tests/... path-shaped literal: word-boundary on the left (so "xtests/foo"
# doesn't match), "tests/" itself, then one or more path segments ending in
# .py — NOT anchored to the start of a string/quote, matches anywhere inside
# a larger run of text (#4006's own lesson — see module docstring).
_PATH_LITERAL_RE = re.compile(r"(?<![\w/])tests/[\w][\w./-]*\.py")

# File types worth scanning for a tests/ path reference — prose (.md), code
# (.py), config/CI (.yml/.yaml), anything text-based a person could type a
# path into. Deliberately broad per the issue's own "docstring / comment /
# string / YAML / Markdown" scope.
_SCAN_SUFFIXES = frozenset({".py", ".md", ".yml", ".yaml", ".rst", ".txt", ".json"})

# CHANGELOG.md is TRACKED (so `git ls-files` does not exclude it the way it
# excludes a gitignored build directory), but it's a historical record — a
# past entry correctly names a path that was real AT THE TIME the entry was
# written, then the repo moved on. Rewriting history to keep a changelog
# "accurate" about the present is backwards; this file's job is to say what
# happened, not what's still true. This is why CHANGELOG.md needs an
# explicit rule while `site/` does not (lead-coder review, #4065): tracked
# vs. gitignored is exactly the line between "needs a rule" and "the
# population definition already handles it."
#
# check_tests_path_literal_reference_baseline.json is THIS gate's own
# committed baseline — it necessarily embeds hundreds of `tests/...py`
# literal strings as DATA (the very things the scan reports), which the
# generic regex-over-text-content scan would otherwise re-match as if they
# were prose/code references and count once per baseline entry per run —
# an entirely self-inflicted, ever-growing false-positive population,
# caught the first time `--write-baseline` was run against itself.
#
# scripts/flat_tests_disposition.json (#3879 S2) is the SAME shape as
# CHANGELOG.md, not the same shape as this gate's own baseline: its
# `moved` entries are keyed on the file's OLD flat path by design — "this
# file used to be at the flat root, now lives in a bucket subdirectory" is
# the record (deliberately not spelled as a literal tests/-shaped example
# here, so this docstring's own prose doesn't become a hit against this
# gate's own scan), and the old path correctly never resolves again.
# Counting that as gate debt would
# charge #3879's own disposition-tracking artifact for doing its job
# (lead-coder review, #4065 follow-up): every file it records as moved
# would cost one baseline entry here, forever, which is backwards for the
# same reason rewriting CHANGELOG.md's history would be.
#
# scripts/flat_tests_arc_population.json (#3879 S5, #4072) is the SAME
# shape again: a FROZEN, point-in-time snapshot of every flat filename
# Stage 0 committed — most of them have since moved into a bucket by
# design, so most of the 1,129 entries never resolve, and never should.
# It carries no `to` field pointing anywhere current to fall back on
# (unlike disposition.json's `moved` entries) — it is pure historical
# population data, read-only, the same class as CHANGELOG.md.
_EXCLUDED_FILES = frozenset({
    "CHANGELOG.md",
    "check_tests_path_literal_reference_baseline.json",
    "flat_tests_disposition.json",
    "flat_tests_arc_population.json",
})


def _iter_scan_files(root: Path = _ROOT):
    """Every TRACKED file worth scanning — `git ls-files`, filtered to
    :data:`_SCAN_SUFFIXES` and :data:`_EXCLUDED_FILES`. See the module
    docstring's "Population" section for why this is `git ls-files` and not
    a directory walk."""
    proc = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True,
    )
    for line in proc.stdout.splitlines():
        rel = Path(line)
        if rel.suffix not in _SCAN_SUFFIXES:
            continue
        if rel.name in _EXCLUDED_FILES:
            continue
        yield root / rel


def _ever_tracked_tests_py(root: Path = _ROOT) -> "set[str]":
    """Every `tests/*.py` path that has EVER been a real tracked file, at
    any point in the repo's full history — the "never-existed" vs. "stale"
    class discriminator (lead-coder review, #4065): a literal naming a path
    that never once existed is an illustrative example or a doc's promise
    that was never built (a doc-accuracy defect worse than staleness, but
    harmless to run — never a real file moving), while a literal naming a
    path that WAS real and now isn't is a genuine stale reference (fix the
    path). Collapsing the two loses exactly the information a future triage
    pass needs and would have to re-measure from scratch."""
    proc = subprocess.run(
        ["git", "log", "--all", "--name-only", "--pretty=format:", "--", "tests/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def offending_references(root: Path = _ROOT) -> "list[tuple[Path, str, int]]":
    """Every (referencing_file, tests/-path-literal, line_number) where the
    literal does NOT resolve to a real file under *root* — the gate's
    entire decision, isolated from CLI/printing so it is directly
    testable."""
    offenders: list[tuple[Path, str, int]] = []
    for path in _iter_scan_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in _PATH_LITERAL_RE.finditer(line):
                literal = m.group(0)
                if not (root / literal).is_file():
                    offenders.append((path, literal, lineno))
    return offenders


def measured_pairs(root: Path = _ROOT) -> "set[tuple[str, str]]":
    """The ``{(referencing_file, literal)}`` set — the ratchet's key,
    deliberately dropping the line number (see module docstring: an
    unrelated edit shifting a line above a reference must not itself flip
    the gate red)."""
    return {
        (str(path.relative_to(root)), literal)
        for path, literal, _lineno in offending_references(root)
    }


def classify(literal: str, ever_tracked: "set[str]") -> str:
    """``"stale"`` if *literal* was ever a real tracked `tests/*.py` file
    (it moved or was deleted — fix the reference), else
    ``"never-existed"`` (an illustrative example, OR a doc naming a test
    that was promised and never built — see :func:`_ever_tracked_tests_py`).
    Not used to decide inclusion — every pair is baselined regardless of
    class; this is recorded metadata for a future triage pass, never a
    filter (lead-coder review, #4065: an exclusion rule needs a machine-
    checkable condition, and "illustrative in context" is a judgment, not
    one — collapsing the classes here would lose exactly the same
    information a filter would have hidden)."""
    return "stale" if literal in ever_tracked else "never-existed"


def load_baseline(path: Path = _BASELINE_PATH) -> "set[tuple[str, str]]":
    data = json.loads(path.read_text(encoding="utf-8"))
    return {(entry["file"], entry["literal"]) for entry in data}


def write_baseline(
    pairs: "set[tuple[str, str]]", ever_tracked: "set[str]", path: Path = _BASELINE_PATH,
) -> None:
    data = [
        {"file": f, "literal": lit, "class": classify(lit, ever_tracked)}
        for f, lit in sorted(pairs)
    ]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def new_pairs(
    measured: "set[tuple[str, str]]", baseline: "set[tuple[str, str]]"
) -> "set[tuple[str, str]]":
    """The ratchet check itself: any measured pair the baseline does not
    already declare is new — a pair leaving the measured set (a fix, or the
    referencing file itself moving/being deleted) is not reported here at
    all, by design (see module docstring)."""
    return measured - baseline


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from a fresh scan instead of checking "
            "against it. Use ONLY after actually fixing/triaging references — "
            "regenerating to silence a new failure defeats the ratchet (see "
            "module docstring)."
        ),
    )
    args = parser.parse_args(argv)

    measured = measured_pairs(_ROOT)

    if args.write_baseline:
        write_baseline(measured, _ever_tracked_tests_py(_ROOT))
        print(f"Wrote {len(measured)} (referencing_file, literal) pair(s) to {_BASELINE_PATH}")
        return 0

    baseline = load_baseline()
    new = new_pairs(measured, baseline)

    if not new:
        print(
            f"tests-path-literal-reference ratchet OK: {len(measured)} reference(s), "
            f"all baselined ({len(baseline)} declared)."
        )
        return 0

    print("tests-path-literal-reference ratchet FAILED:\n", file=sys.stderr)
    print(
        f"{len(new)} new (referencing_file, literal) pair(s) not in the baseline "
        f"({_BASELINE_PATH.relative_to(_ROOT)}) — a tests/ file reference that no "
        "longer resolves and wasn't there before:",
        file=sys.stderr,
    )
    for file, literal in sorted(new):
        print(f"  {file}: {literal}", file=sys.stderr)
    print(
        "\nUpdate the reference to the file's current location (or remove it "
        "if the file was deleted). If the literal is a deliberate "
        "illustrative example (not a real path), that's fine too — but say "
        "so and regenerate the baseline (--write-baseline) rather than "
        "leaving it unexplained.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
