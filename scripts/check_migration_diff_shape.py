#!/usr/bin/env python3
"""#3879 Stage 1 M1 (PR-0) — a migration PR moves tests, never rewrites them.

INVARIANT: a Stage-1 migration PR's diff contains ONLY byte-identical
``git mv`` — never a rewrite dressed as a move. Owner's question that
motivates this gate:

    移動するテストコードが、ファイル名変更でなく書写しをすることもあるの？
    (Can the test code being moved get REWRITTEN, rather than just renamed?)

Yes — an agent told "move this file" can satisfy the instruction by writing
a new file at the destination and deleting the old one, without ever running
``git mv``. Since #3879's whole migration argument rests on "the move costs
zero bytes changed, so review cost is zero" (see #3879 comment 5228674957),
a rewrite disguised as a move is exactly the failure this gate exists to
catch — "the content didn't change" must be a property the DIFF'S SHAPE
proves, not something a human declares.

## What ``-M100%`` actually detects (verified, not assumed)

``git diff -M100%`` reports a rename ONLY when the moved file's content is
BYTE-IDENTICAL — even a single added character breaks the ``R100``
classification and the pair instead appears as two independent lines,
``A <new path>`` / ``D <old path>`` (verified directly: a 3-line file with
one line appended at the destination, old deleted, reports exactly that
shape, never a lower-similarity ``R09x``).

Architect's own measurement (#3879 comment 5228674957, a real detached
worktree, not assumed): ``diff.renameLimit`` (git's cap on the O(N×M)
similarity search for INEXACT renames) never engages here, at any scale up
to the full 1,126-file migration in one commit — an exact-content rename is
resolved by a blob-hash match BEFORE the expensive similarity search runs,
so it is never subject to that limit. This gate does not need to raise it.

## Activation — the diff decides, never a declaration (lead-coder's #3885
## review correction, after the first version of this gate fired on EVERY
## PR touching tests/, including ordinary Q3/Q4 assert-repair PRs)

This gate is INACTIVE — exits 0 immediately, checks nothing — unless the
diff shows real evidence of a tests/ migration: a pure (``R100``) rename
under ``tests/``, OR a brand-new ``.py`` file landing inside a ``tests/``
SUBDIRECTORY (see :func:`is_new_file_in_tests_subdir`). A PR with neither
signal is not a migration PR, whatever it claims to be (a label, a branch
name, a PR-body declaration): the whole audit this session has been
running finds that exact "declaration ≠ reality" shape everywhere else
(Tier labels no one earned, "falsify done" claims that were only reasoned
through, a hand-editable baseline) and a self-declared "this is a
migration PR" signal would just be one more instance of it. So the diff's
own content is the only signal read.

★ Corrected in review, not assumed correct from the design description:
this module's FIRST version claimed "a 'migration' whose renames
`-M100%` failed to detect at all makes this gate inactive too, but Stage
0's ratchet independently rejects that shape — the two gates catch it
between them." **That claim was false, and neither this author nor
lead-coder (who wrote it first, in the #3885 design comment) had checked
it before it shipped into this docstring** — `flat_tests_ratchet.py`'s
`measured_flat_files()` globs `tests/*.py` NON-recursively (its own
docstring says so explicitly: "a file already moved into a subdirectory
is correctly absent"), so a fully-rewritten file landing in
`tests/<pkg>/` — exactly where a real migration destination is — was
invisible to BOTH gates, not caught by either. Fixed by adding the second
activation signal above rather than by weakening the claim to a caveat;
the class of "I transcribed a claim instead of checking it" is the same
one this whole #3872/#3879 arc has been finding all night, this time in
the gate meant to guard against exactly that shape.

## Allowed diff lines, once active (everything else in scope is rejected)

- ``R100  <old>  <new>`` — a pure, byte-identical rename.
- ``A  tests/<pkg>/__init__.py`` — a NEW, EMPTY package marker (a migration
  PR creating a subdirectory needs one; content is checked, not just the
  path, so a rewrite masquerading as a placeholder is still caught).
- ``M  scripts/flat_tests_baseline.json`` — the Stage-0 ratchet baseline
  SHRINKING as moved names drop out (#3883 already permits and expects
  this — not re-forbidden here).

Everything else UNDER ``tests/`` (or the baseline file) — any other
``A``/``M``/``D``, or a rename below 100% similarity — fails the gate. A
change entirely OUTSIDE ``tests/`` and not the baseline is left alone (not
this gate's concern, whatever else may check it).

## Lifespan — this gate is temporary, unlike Stage 0's ratchet

Stage 0's ratchet (``flat_tests_ratchet.py``) is permanent: it must keep
rejecting new flat files long after Stage 1 finishes. THIS gate is the
opposite — it exists only to police Stage 1's own migration PRs and would
wrongly block a legitimate, unrelated PR that deletes a Tier-4 test (a real
``D`` with no matching content elsewhere) once Stage 1 is done. Same
lifespan discipline as ``tests/scaffold/``'s ``triggered_by``/``removed_by``
convention: ``triggered_by: #3879 Stage 1``; remove this gate (and its
workflow file) in the PR that lands the LAST Stage-1 migration.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ALLOWED_MODIFIED_PATHS = frozenset({"scripts/flat_tests_baseline.json"})
_INIT_SUFFIX = "__init__.py"


def diff_name_status(base: str, root: Path = _ROOT) -> "list[str]":
    """Raw ``git diff -M100% --name-status <base>...HEAD`` lines.

    Three-dot (``base...HEAD``, merge-base diff) — the same comparison
    GitHub's own PR diff view uses, so this gate agrees with what a reviewer
    actually sees, not with whatever line HEAD happens to have crossed."""
    proc = subprocess.run(
        ["git", "diff", "-M100%", "--name-status", f"{base}...HEAD"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def blob_at_head(path: str, root: Path = _ROOT) -> "bytes | None":
    """Content of *path* at HEAD, or ``None`` if it doesn't exist there
    (defensive — a well-formed ``A`` line always has one, but a gate must
    not crash on a git-format surprise it hasn't seen)."""
    proc = subprocess.run(
        ["git", "show", f"HEAD:{path}"], cwd=root, capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def is_tests_rename(line: str) -> bool:
    """Whether *line* is a pure (R100) rename whose NEW path lands under
    ``tests/`` — one of the two activation signals for the whole gate (see
    :func:`gate_is_active`)."""
    parts = line.split("\t")
    if not parts[0].startswith("R"):
        return False
    similarity = parts[0][1:]
    return similarity == "100" and len(parts) == 3 and parts[2].startswith("tests/")


def is_new_file_in_tests_subdir(line: str) -> bool:
    """Whether *line* is a brand-new ``.py`` file landing inside a
    ``tests/`` SUBDIRECTORY (``tests/<pkg>/...``, not a direct child) — the
    OTHER activation signal, and the fix to a real gap found in this gate's
    own first version (#3885 review, lead-coder + this author both
    transcribed an unverified claim that Stage 0's ratchet would catch this
    shape on its own — checked directly and it does not:
    ``flat_tests_ratchet.measured_flat_files()`` globs ``tests/*.py``
    NON-recursively, by its own docstring's own words, so a file already
    landed in a subdirectory is invisible to it). A fully-rewritten "move"
    (git's ``-M100%`` failing to detect ANY rename because every byte
    changed) produces exactly this shape: a new ``A`` line under
    ``tests/<pkg>/``, with no matching rename anywhere — this is what makes
    that case activate the gate, where :func:`is_tests_rename` alone would
    have missed it entirely."""
    parts = line.split("\t")
    if parts[0] != "A" or len(parts) != 2:
        return False
    path = parts[1]
    if not path.endswith(".py"):
        return False
    # "tests/<subdir>/..." has at least 3 slash-separated segments; a direct
    # child ("tests/test_a.py") has exactly 2 and is Stage 0's territory,
    # not this gate's — an empty __init__.py addition is legitimately
    # allowed once active (see offending_lines), so it must still be able
    # to trigger activation here too.
    return path.count("/") >= 2 and path.startswith("tests/")


def gate_is_active(lines: "list[str]") -> bool:
    """The gate applies ONLY to a PR whose diff shows REAL evidence of a
    tests/ migration — never a label, a branch-name convention, or any
    other DECLARATION of "this is a migration PR" (lead-coder's #3885
    review correction: a self-declared signal is exactly the
    declaration-vs-reality gap this whole audit exists to close — Tier
    labels, "falsify done" claims, hand-edited baselines, all the same
    shape). Two signals, either one activates:

    - :func:`is_tests_rename` — a pure R100 rename under ``tests/``.
    - :func:`is_new_file_in_tests_subdir` — a brand-new ``.py`` landing in a
      ``tests/`` SUBDIRECTORY, which is what a FULLY-rewritten "move" (zero
      bytes shared, so ``-M100%`` detects no rename at all) looks like —
      without this second signal, that exact shape passed both this gate
      (inactive, no rename) AND Stage 0's ratchet (non-recursive glob, blind
      to subdirectories) untouched. Found and fixed in review, not assumed
      correct from the design description (see module's own account of the
      mistake).

    No signal anywhere → this PR isn't touching tests/'s Stage-1 migration
    at all, whatever it claims to be, and an ordinary Q3/Q4 assert-repair PR
    passes through untouched."""
    return any(is_tests_rename(line) or is_new_file_in_tests_subdir(line) for line in lines)


def _in_scope(line: str) -> bool:
    """Whether *line* is something this gate evaluates at all, once active:
    a path under ``tests/`` (either side of a rename), or the Stage-0
    baseline file. A completely unrelated change elsewhere in the same PR
    is left to whatever OTHER gate cares about it — this one only judges
    the shape of the tests/ move itself."""
    parts = line.split("\t")
    paths = parts[1:]
    if any(p in _ALLOWED_MODIFIED_PATHS for p in paths):
        return True
    return any(p.startswith("tests/") for p in paths)


def offending_lines(lines: "list[str]", root: Path = _ROOT) -> "list[str]":
    """Every in-scope diff line that is NOT one of the three allowed shapes
    — the gate's entire decision, isolated from I/O so it is directly
    testable against a hand-built line list. Only called once
    :func:`gate_is_active` has confirmed this PR is a migration PR at all;
    an out-of-scope line (outside ``tests/``, not the baseline) is skipped
    here, not flagged — see :func:`_in_scope`."""
    offenders = []
    for line in lines:
        if not _in_scope(line):
            continue
        parts = line.split("\t")
        status = parts[0]

        if status.startswith("R"):
            # e.g. "R100" — the percentage is everything after "R".
            similarity = status[1:]
            if similarity == "100" and len(parts) == 3:
                continue
            offenders.append(line)
            continue

        if status == "A" and len(parts) == 2:
            path = parts[1]
            if path.endswith("/" + _INIT_SUFFIX) or path == _INIT_SUFFIX:
                content = blob_at_head(path, root)
                if content == b"":
                    continue
            offenders.append(line)
            continue

        if status == "M" and len(parts) == 2 and parts[1] in _ALLOWED_MODIFIED_PATHS:
            continue

        offenders.append(line)

    return offenders


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--base", required=True,
        help="base ref to diff against (e.g. origin/main, or a PR's merge-base sha).",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    lines = diff_name_status(args.base)

    if not gate_is_active(lines):
        print(
            "OK: gate inactive — this PR's diff contains no pure (R100) "
            "rename under tests/, so it is not a migration PR (whatever it "
            "claims to be — the diff's own content is the only signal this "
            "gate reads)."
        )
        return 0

    offenders = offending_lines(lines)

    if not offenders:
        print(
            f"OK: {len(lines)} diff line(s) vs {args.base}, all pure renames / "
            "empty __init__.py additions / the Stage-0 baseline shrinking."
        )
        return 0

    print("migration-diff-shape gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} diff line(s) are not a pure move (R100), an empty "
        "tests/<pkg>/__init__.py, or the Stage-0 baseline:",
        file=sys.stderr,
    )
    for line in offenders:
        print(f"  {line}", file=sys.stderr)
    print(
        "\nA Stage-1 migration PR moves tests, never edits their content — "
        "`git mv` the file(s) above rather than recreating them at the new "
        "path, and split any real content change into a SEPARATE PR (this "
        "gate cannot tell 'legitimate unrelated fix' from 'the exact rewrite "
        "this gate exists to catch' — it rejects both on purpose).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
