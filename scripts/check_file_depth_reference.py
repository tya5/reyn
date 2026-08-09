#!/usr/bin/env python3
"""#3995/#4002 — the "new file, static-time" half of the __file__-depth
class. NOT the same mechanism as `check_migration_diff_shape.py`'s a′
(#4002 explicitly retracted "1機構" — see that module's own docstring for
the two retractions this arc went through the same night).

## Why this is a DIFFERENT, narrower mechanism than a′ — not a shared one

a′ runs against a file that has ALREADY MOVED (a real checkout, the new
path exists on disk) — it can resolve every ``__file__``-rooted expression
at the file's REAL location and ask a ground-truth question: does the
target still exist? This gate runs against a file that is NOT moving at
all (freshly added, or simply present on the current tree) — there is no
"before" and "after" to compare, so architect's point stands: "the safety
of ``.parent / X`` is a property of the MOVE, not of the source text
alone" — a static check literally cannot answer a′'s question, because
there is no move for it to answer about.

So this gate does not try to answer the same question — it applies a
narrower, STRUCTURAL proxy instead: does a ``__file__``-rooted expression,
resolved using the file's OWN CURRENT location, land on (a) ``tests/``
itself or anything ABOVE it (an unambiguous escape — reaching outside
``tests/`` entirely, e.g. the repo root, is never something an individual
file carries with it), or (b) one of ``tests/``'s CURRENT direct child
directories (the #4002 shape — ``_support``, ``fixtures``, and whatever
else currently sits directly under ``tests/``, discovered from the real
filesystem, never a hardcoded name list)? Both are locations that do not
travel with an individual file's future move, by the same reasoning #4002
found for ``_support`` specifically — generalized structurally (via a real
path comparison) rather than via a curated name list, so a name this
gate has never seen before (a NEW ``tests/`` subdirectory added tomorrow)
is still caught without needing this file edited.

This is a DETERRENT against reintroducing the pattern this arc spent one
night correcting twice, not a full replacement for a′'s ground-truth
check — a plain co-located reference (``Path(__file__).parent /
"my_own_fixture.json"``, staying inside the file's own directory and never
touching ``tests/`` or one of its direct children) is not flagged, the
same as it always was.

★ One deliberate over-flag: for a FILE THAT ITSELF LIVES DIRECTLY IN
``tests/``, its own directory already IS ``tests_dir`` — so ANY ``.parent
/ <name>`` reference it makes is structurally IDENTICAL to a reference to
a ``tests/``-root peer directory (case (b) above), whatever the name. This
is architect's own "undecidable from source text alone" finding, applying
here in its sharpest form: there is no way to tell, from a flat file's
source alone, whether ``<name>`` means "a private, co-located fixture" or
"the shared, fixed ``_support``/``fixtures`` directory" — and #4002's own
real confirmed instance (``test_2608_h1_mcp_resource_updated_hook.py``)
WAS exactly this shape, a flat file. So this gate flags it either way,
erring toward the false positive over the false negative — a genuine
per-test co-located fixture pattern, if ever needed, is expected to live
in a subdirectory instead (where the ambiguity does not arise, see the
first test case in the companion test file).

## Why baseline 0 — and the premise this gate's OWN whole-tree scan falsified

The FIRST version of this docstring claimed #3990 + #3997 + #3998 + #4002
had already brought the population to zero — that claim was itself wrong,
caught by this gate's own whole-tree scan BEFORE this PR merged: #4002
fixed exactly ONE file (the single instance in scope for the ``hooks``
bucket it was prepping), not the whole class. Running the newly-built
predicate against the real, current tree — the same predicate this gate
now enforces — found **11 more files** carrying the identical, unfixed
``Path(__file__).parent / "_support"`` pattern (lead-coder's own
independent measurement converges on 16 total instances of this shape
across the repo; this PR's 11 is the subset still in ``tests/`` root
scope after #4002's fix and #4002's own hooks-bucket move — see #3995's
issue thread for the full reconciliation). **This is the gate working
correctly, not a design failure**: a static gate finding real, pre-
existing violations DURING its own construction is exactly the outcome a
genuinely zero-baseline gate is supposed to produce when the "zero"
premise turns out to be wrong — the alternative (shipping the gate
without running it against the real tree first) would have landed it
already red.

This PR fixes all 11 in the same commit, using the IDENTICAL pattern
#4002 already established (``REPO_ROOT / "tests" / "_support"``,
content-only, no rename) — so by the time this gate lands, the baseline-0
claim is true again, verified by the gate's own test
(``test_the_real_repo_tree_is_currently_clean``) rather than assumed.
Going forward, this remains a true ratchet with no grandfather clause:
every violation this gate finds from here on is a fresh regression
against this NOW-verified-zero starting point, not inherited debt.

## The one structural exclusion: ``tests/conftest.py``

pytest requires ``conftest.py`` to live at a fixed location relative to
the tests it configures — the one file in ``tests/`` that STRUCTURALLY
never moves (M4's own migration leaves it in place), so a reference there
carries none of the risk this gate exists to catch. Every OTHER ``tests/``
file is in scope, subdirectories included.
"""
from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from _file_depth_predicate import parse_file_relative_targets  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"
_STRUCTURALLY_EXEMPT = frozenset({"conftest.py"})


def _references_a_fixed_tests_location(path: Path, tests_dir: Path) -> bool:
    """#4002: does *path*'s content contain a ``__file__``-rooted
    expression that, resolved using *path*'s OWN current location, reaches
    ``tests_dir`` itself (or above it — a repo-root-or-higher escape), or
    lands on one of ``tests_dir``'s CURRENT direct children? Both are
    locations that do not travel with an individual file's future move.
    The child-directory check needs no name list: it is a structural
    comparison (``target.parent == tests_dir``) against the real
    filesystem, so a not-yet-existing future ``tests/`` subdirectory is
    covered automatically, the same way ``_support``/``fixtures`` were
    found without either being hardcoded anywhere."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    for target in parse_file_relative_targets(content, path):
        # (a) the target is not tests_dir itself and not nested anywhere
        # inside it — an unambiguous escape (repo root, a sibling of
        # tests_dir, anything outside the tests/ subtree entirely).
        if tests_dir not in (target, *target.parents):
            return True
        # (b) the target IS still inside tests/, but sits directly at its
        # root level — a fixed, shared peer directory (_support,
        # fixtures, ...) rather than something nested under the file's
        # own subdirectory. FS-derived: no name list, just a structural
        # comparison against the real tests_dir. Excludes `target == path`
        # (a bare `Path(__file__)`/`.resolve()`, no navigation at all) —
        # for a file living directly in tests/, its OWN path trivially has
        # tests_dir as its parent; that is not "referencing a tests-root
        # peer directory", it's just where the file itself happens to
        # live (a real false positive this exclusion fixes: a marker-walk
        # helper doing `for ancestor in Path(__file__).resolve().parents:
        # ...` starts from the bare resolved file, which this predicate
        # must not itself flag before the loop even navigates anywhere).
        if target != path and target.parent == tests_dir:
            return True
    return False


def offending_files(tests_dir: Path = _TESTS_DIR) -> "list[Path]":
    """Every ``.py`` file under *tests_dir* (recursive) matching
    :func:`_references_a_fixed_tests_location` — the gate's entire
    decision, isolated from CLI/printing so it is directly testable.
    ``conftest.py`` (any depth — pytest resolves the nearest one to the
    file being collected) is excluded: it is the one file class that
    structurally never moves, see module docstring."""
    offenders = []
    for path in sorted(tests_dir.rglob("*.py")):
        if path.name in _STRUCTURALLY_EXEMPT:
            continue
        if _references_a_fixed_tests_location(path, tests_dir):
            offenders.append(path)
    return offenders


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-tree scan against a baseline of zero
    offenders = offending_files(_TESTS_DIR)

    if not offenders:
        print(
            "OK: no tests/ file references tests/ itself, above it, or one "
            "of its direct children via __file__."
        )
        return 0

    print("file-depth-reference gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} file(s) under tests/ contain a __file__-rooted "
        "expression that reaches tests/ itself (or above it), or lands on "
        "one of tests/'s direct child directories (e.g. _support, "
        "fixtures) — this SILENTLY BREAKS the moment the file is moved to "
        "a different depth (the exact M4 failure class, #3989/#3994/#4002), "
        "and this gate's own starting population is zero, so any hit here "
        "is a new regression, not inherited debt:",
        file=sys.stderr,
    )
    for path in offenders:
        print(f"  {path.relative_to(_ROOT)}", file=sys.stderr)
    print(
        "\nUse the marker-walk `tests._support.paths.REPO_ROOT` instead — "
        "depth-independent, correct at any location including its own, "
        "permanently. (`tests/conftest.py` is the one structural exception: "
        "it never moves, so a reference there is exempt.)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
