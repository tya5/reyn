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

## #4019 — (b) generalized from "direct child of tests/" to "outside the
## file's own home subdirectory" (lead-coder + tui-coder, same night)

The FIRST version of (b) only caught a target sitting DIRECTLY under
``tests_dir`` (``target.parent == tests_dir``) — real for #4002's
``Path(__file__).parent / "_support"`` (one join, lands one level under
``tests/``), but tui-coder's pre-move audit found a real miss:
``tests/test_fp0063_arc_witness.py``'s ``Path(__file__).parent / "fixtures"
/ "llm" / "fp0063_arc_witness"`` resolves to ``tests/fixtures/llm/
fp0063_arc_witness`` — a target NESTED *inside* the peer directory
``tests/fixtures``, not sitting directly under ``tests_dir`` itself, so
the old (b) (checking only ``target.parent == tests_dir``) never fired,
even though the reference is exactly as position-dependent: moving the
file to ``tests/runtime/`` makes it resolve to ``tests/runtime/fixtures/
llm/...``, which does not exist.

The generalization: instead of asking "is the target a direct child of
``tests_dir``", ask "does the target stay inside the file's OWN home
subdirectory" — where "home" is the first path component under
``tests_dir`` (``tests_dir`` itself for a flat file; ``tests_dir/runtime``
for a file already living in ``tests/runtime/...``). A target inside the
file's own home travels with it (the M4 migration moves a file's whole
home subdirectory as a unit, not individual files within it); a target
OUTSIDE the home — whether one level under ``tests_dir`` or nested many
levels inside some OTHER top-level peer — does not. The old direct-child
check is the DEPTH-1 special case of this same rule (a flat file's own
home already equals ``tests_dir``, so a direct-child target is trivially
"outside home" too) — see :func:`_own_home` / :func:`_references_a_fixed_tests_location`.

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

## #4019 (review finding) — a WITHIN-home reference can still be silently
## wrong, and neither (a)/(b) above nor a′ can see it

(a)/(b) are purely STRUCTURAL/positional checks — "does this look like it
reaches outside home" — never an EXISTENCE check, by design (a′ owns
existence-checking, at real move time). That leaves a real gap: a
``__file__``-rooted reference that stays structurally INSIDE its own home
but points at something that plain doesn't exist — e.g. a file already
resolved into ``tests/dev/`` whose own reference computes ``tests/dev/
fixtures/llm`` (structurally fine, "inside home") when the real, intended
directory is ``tests/fixtures/llm`` (a stale reference from BEFORE the
file's own last move). Neither (a)/(b) (position looks fine) nor a′ (only
runs against a migration PR's OWN diff, not retroactively against
already-merged history) can catch this — confirmed as a REAL, not
hypothetical, gap: ``tests/dev/test_replay_fixture_no_stacking_3634.py``
had exactly this stale reference, silently collecting ZERO fixture files
and skipping every parametrized case instead of failing (found and fixed
in the same PR that added this section).

**Why not a blanket existence-assert on every resolved target**: an
operator's own review (lead-coder, #4019) named the concrete risk
directly — plenty of legitimate ``__file__``-rooted paths name a
directory a test CREATES at runtime (a per-test output dir, a staging
area) rather than one that must already exist on disk at STATIC-SCAN
time. A naive "the target must exist right now" check would false-positive
on that whole, common pattern.

**The fix: :func:`scripts._file_depth_predicate.module_level_glob_roots`**
— narrowly scoped to the exact shape that broke: a ``__file__``-rooted
directory used, AT MODULE LEVEL (import time, never inside a function or
class body), as the base of a ``.glob(...)``/``.rglob(...)`` call. A
runtime-created output directory is, by construction, always built
INSIDE a function body (nothing has run yet at bare module scope) — so
restricting to module-level glob roots specifically targets "eager
fixture discovery at import time" (this gate's real broken instance)
while structurally excluding the runtime-directory false-positive risk
the review flagged. See :func:`_missing_module_level_glob_roots`.
"""
from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from _file_depth_predicate import (  # noqa: E402
    module_level_glob_roots,
    parse_file_relative_targets,
)

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"
_STRUCTURALLY_EXEMPT = frozenset({"conftest.py"})


def _own_home(path: Path, tests_dir: Path) -> "Path | None":
    """The subdirectory that travels with *path* as a unit under the M4
    migration — ``tests_dir/<first component>`` for a file already living
    in a subdirectory (``tests/runtime/x.py`` → ``tests/runtime``: the
    WHOLE bucket moves together, so anything nested inside it is safe).

    ``None`` for a flat file (``tests/x.py``, ``path.parent == tests_dir``)
    — a flat file has no established subdirectory yet (it hasn't been
    bucketed), so nothing has been shown to travel with it except the file
    itself and its own bare containing directory (``tests_dir`` — handled
    separately, see :func:`_references_a_fixed_tests_location`'s
    ``target == path.parent`` exclusion). Returning ``tests_dir`` itself
    here would make "inside home" trivially true for anything anywhere
    under ``tests/``, defeating the check entirely for exactly the file
    shape #4019's real instance was (a flat file's ``Path(__file__).parent
    / "fixtures" / "llm" / "..."`` — one join beyond the bare ``.parent``
    this function does not cover)."""
    if path.parent == tests_dir:
        return None
    rel = path.parent.relative_to(tests_dir)
    return tests_dir / rel.parts[0]


def _references_a_fixed_tests_location(path: Path, tests_dir: Path) -> bool:
    """#4002/#4019: does *path*'s content contain a ``__file__``-rooted
    expression that, resolved using *path*'s OWN current location, reaches
    outside ``tests_dir`` entirely (a repo-root-or-higher escape), or
    lands anywhere OUTSIDE *path*'s own home subdirectory (see
    :func:`_own_home`) — a peer reference, whether it sits directly under
    ``tests_dir`` (#4002's original ``.parent / "_support"`` shape) or
    nested several levels inside a peer directory (#4019's ``.parent /
    "fixtures" / "llm" / "..."`` shape, missed by the FIRST version of
    this check, which only asked "is target a DIRECT child of tests_dir").
    Both are locations that do not travel with an individual file's future
    move. Needs no name list: it is a structural comparison against the
    real filesystem (the file's own current path + the real ``tests_dir``),
    so a not-yet-existing future ``tests/`` subdirectory is covered
    automatically, the same way ``_support``/``fixtures`` were found
    without either being hardcoded anywhere."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    home = _own_home(path, tests_dir)
    for target in parse_file_relative_targets(content, path):
        # Trivial self-references are always safe, flat or nested: the
        # bare file itself (`Path(__file__)`/`.resolve()`, no navigation),
        # and the bare containing directory (`Path(__file__).parent`
        # alone, no further join) — "my own directory" is not "a peer",
        # regardless of whether that directory happens to be an
        # established bucket or still `tests_dir` itself for a flat file.
        if target in (path, path.parent):
            continue
        # (a) the target is not tests_dir itself and not nested anywhere
        # inside it — an unambiguous escape (repo root, a sibling of
        # tests_dir, anything outside the tests/ subtree entirely).
        if tests_dir not in (target, *target.parents):
            return True
        # (b) the target descends past the bare containing directory
        # (some `.parent / X` join, at any further depth) INTO territory
        # outside the file's own established home. For a NESTED file
        # (home is its own bucket), anything under that bucket is safe —
        # the whole bucket moves as a unit. For a FLAT file (home is
        # `None` — no bucket established yet), ANY descent past the bare
        # `.parent` already excluded above is a peer reference: nothing
        # has been shown to travel with a flat file except itself.
        if home is None or home not in (target, *target.parents):
            return True
    return False


def _has_a_missing_module_level_glob_root(path: Path) -> bool:
    """#4019 (review finding): does *path* use a ``__file__``-rooted
    directory, AT MODULE LEVEL, as a ``.glob(...)``/``.rglob(...)`` root
    that does not exist on disk right now? See the module docstring's
    "WITHIN-home reference can still be silently wrong" section for why
    this is scoped to module-level roots specifically (excludes the
    runtime-created-directory false-positive risk) and why it is a
    SEPARATE check from :func:`_references_a_fixed_tests_location`
    (existence is a′'s question; this is the one narrow slice of it a
    static check can safely still ask)."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    for root in module_level_glob_roots(content, path):
        if not root.is_dir():
            return True
    return False


def offending_files(tests_dir: Path = _TESTS_DIR) -> "list[Path]":
    """Every ``.py`` file under *tests_dir* (recursive) matching
    :func:`_references_a_fixed_tests_location` (the peer-directory proxy)
    OR :func:`_has_a_missing_module_level_glob_root` (#4019's narrow
    existence check) — the gate's entire decision, isolated from
    CLI/printing so it is directly testable. ``conftest.py`` (any depth —
    pytest resolves the nearest one to the file being collected) is
    excluded: it is the one file class that structurally never moves, see
    module docstring."""
    offenders = []
    for path in sorted(tests_dir.rglob("*.py")):
        if path.name in _STRUCTURALLY_EXEMPT:
            continue
        if _references_a_fixed_tests_location(
            path, tests_dir
        ) or _has_a_missing_module_level_glob_root(path):
            offenders.append(path)
    return offenders


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-tree scan against a baseline of zero
    offenders = offending_files(_TESTS_DIR)

    if not offenders:
        print(
            "OK: no tests/ file references tests/ itself, above it, or "
            "outside its own home subdirectory via __file__, and no "
            "module-level glob root points at a missing directory."
        )
        return 0

    peer_refs = [
        p for p in offenders if _references_a_fixed_tests_location(p, _TESTS_DIR)
    ]
    missing_roots = [
        p for p in offenders if _has_a_missing_module_level_glob_root(p)
    ]

    print("file-depth-reference gate FAILED:\n", file=sys.stderr)

    if peer_refs:
        print(
            f"{len(peer_refs)} file(s) contain a __file__-rooted expression "
            "that reaches tests/ itself (or above it), or lands outside the "
            "file's own home subdirectory — at any depth, not just directly "
            "under tests/ (e.g. _support, fixtures/llm/...) — this SILENTLY "
            "BREAKS the moment the file is moved to a different depth (the "
            "exact M4 failure class, #3989/#3994/#4002/#4019):",
            file=sys.stderr,
        )
        for path in peer_refs:
            print(f"  {path.relative_to(_ROOT)}", file=sys.stderr)
        print(
            "\nUse the marker-walk `tests._support.paths.REPO_ROOT` instead "
            "— depth-independent, correct at any location including its "
            "own, permanently. (`tests/conftest.py` is the one structural "
            "exception: it never moves, so a reference there is exempt.)",
            file=sys.stderr,
        )

    if missing_roots:
        print(
            f"\n{len(missing_roots)} file(s) use a __file__-rooted "
            "directory, AT MODULE LEVEL, as a .glob()/.rglob() root that "
            "does not exist on disk — a stale reference (often left behind "
            "by an EARLIER move), silently collecting zero files instead "
            "of failing (#4019's real instance: tests/dev/test_replay_"
            "fixture_no_stacking_3634.py silently skipped every "
            "parametrized case):",
            file=sys.stderr,
        )
        for path in missing_roots:
            print(f"  {path.relative_to(_ROOT)}", file=sys.stderr)

    print(
        "\nThis gate's own starting population is zero, so any hit here is "
        "a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
