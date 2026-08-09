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
under ``tests/``, OR a brand-new ``.py`` file APPEARING inside a ``tests/``
subdirectory WHILE a ``.py`` DISAPPEARS from somewhere else under
``tests/`` (see :func:`gate_is_active`) — "appeared alone" is NOT enough
(see the twice-corrected account below); a PR with neither signal is not a
migration PR, whatever it claims to be (a label, a branch name, a PR-body
declaration): the whole audit this session has been running finds that
exact "declaration ≠ reality" shape everywhere else (Tier labels no one
earned, "falsify done" claims that were only reasoned through, a
hand-editable baseline) and a self-declared "this is a migration PR"
signal would just be one more instance of it. So the diff's own content is
the only signal read.

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
``A``/``M``/``D``, a rename below 100% similarity (which, with ``-C
--find-copies-harder`` added for #3909, is what a rewrite-disguised-as-a-move
now reports as — ``R099``, not the plain ``A``/``D`` pair an ``-M100%``-only
diff would show — still rejected the same way, only similarity ``"100"`` is
ever allowed), or any ``C`` (copy) line — fails the gate. A change entirely
OUTSIDE ``tests/`` and not the baseline is left alone (not this gate's
concern, whatever else may check it).

## #3909 — the hole this gate had: copy without deleting the original

architect measured three real diff shapes against the ``-M100%``-only
version of this gate (issue #3879 comment 5229557446): a proper
``git mv`` passes, a rewrite-with-the-original-deleted is caught (the ``A``
+ ``D`` pair), but a rewrite that COPIES to the destination and leaves the
original in place passes silently — nothing was deleted for either
activation signal to react to, so ``gate_is_active`` stayed ``False`` and
the copy was never even evaluated. The failure shape is the worst kind:
the same test content exists at two paths, both green, and nothing says
which one is canonical.

A "total ``tests/`` ``.py`` count is unchanged" gate — the first shape
considered — was rejected: it also fires on the single most common
operation in this repo, adding an ordinary new test (issue #3909 body has
the measured table). The chosen fix instead detects the property that
actually distinguishes a copy-left-behind from an ordinary new test: git's
own ``-C --find-copies-harder`` already computes "this new file's content
matches an existing file elsewhere in the tree" — an ordinary new test
shares no content with anything, so it never gets a ``C`` line; a
copy-left-behind always does. See :func:`is_tests_copy` and
:func:`gate_is_active`'s third signal.

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
    """Raw ``git diff -M100% -C --find-copies-harder --name-status
    <base>...HEAD`` lines.

    ``-C --find-copies-harder`` (added for #3909) does not change how ``①``
    a proper move or ``②`` a rewrite-disguised-as-a-move are classified —
    verified directly (a real throwaway repo, not assumed): a pure
    ``git mv`` still reports plain ``R100``, and a 1-line-appended "rewrite
    disguised as a move, original deleted" still reports as a rename
    (``R099``, not the ``-M100%``-only run's ``A``/``D`` pair — a real
    behavior shift, but harmless: ``offending_lines`` only ever allowed
    ``similarity == "100"``, so an ``R099`` line still falls through to
    "offender" exactly as its old ``A``/``D`` shape did). What ``-C`` adds
    is detecting ``③`` — a copy that leaves the ORIGINAL file in place
    (``C100  <old>  <new>``), invisible to ``-M100%`` alone since nothing
    was deleted for it to pair against. Three-dot (``base...HEAD``,
    merge-base diff) — the same comparison GitHub's own PR diff view uses,
    so this gate agrees with what a reviewer actually sees, not with
    whatever line HEAD happens to have crossed."""
    proc = subprocess.run(
        [
            "git", "diff", "-M100%", "-C", "--find-copies-harder",
            "--name-status", f"{base}...HEAD",
        ],
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
    """Whether *line* is ANY rename (any similarity — not just R100) whose
    NEW path lands under ``tests/`` — one of the activation signals for the
    whole gate (see :func:`gate_is_active`).

    ★ Widened from "R100 only" to "any similarity" during #3909's own
    falsify-verification: adding ``-C --find-copies-harder`` to
    :func:`diff_name_status` (needed to detect #3909's copy-left-behind
    hole) has a side effect nobody predicted from the design alone — git's
    broadened similarity search now classifies a rewrite-disguised-as-move
    (one appended line, original deleted) as a LOW-similarity rename
    (``R075`` in one measured case), not the ``A``/``D`` pair the
    ``-M100%``-only gate used to see. An activation check requiring
    ``similarity == "100"`` never sees that line at all — a genuine
    regression this test file's own falsify-verification caught (a
    pre-existing test started failing after #3909's diff-command change,
    not a hypothetical). Activation only needs to know "a rename touched
    tests/" — whether it's ALLOWED (only similarity ``"100"`` is) remains
    :func:`offending_lines`'s job, unaffected by this widening."""
    parts = line.split("\t")
    if not parts[0].startswith("R"):
        return False
    return len(parts) == 3 and parts[2].startswith("tests/")


def is_new_file_in_tests_subdir(line: str) -> bool:
    """Whether *line* is a brand-new ``.py`` file landing inside a
    ``tests/`` SUBDIRECTORY (``tests/<pkg>/...``, not a direct child) — HALF
    of the second activation signal (see :func:`gate_is_active`; the other
    half, :func:`is_tests_deletion`, is required TOO — "appeared" alone
    matches an ordinary new-test-addition PR just as well as a
    fully-rewritten move, and this gate rejected its OWN introducing PR
    (which only adds a test file) before that was caught)."""
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


def is_tests_copy(line: str) -> bool:
    """Whether *line* is a ``C`` (copy) status line touching ``tests/`` on
    either side — the third activation signal, for #3909's remaining hole:
    "copy the file to the destination, but forget to delete the original"
    passes ``is_tests_rename`` (no ``R`` line — nothing was deleted for git
    to pair against) AND ``is_new_file_in_tests_subdir`` + ``is_tests_deletion``
    together (no ``D`` line either, by construction — that's the whole bug).
    Verified directly (real throwaway repo): with ``-C --find-copies-harder``
    added to :func:`diff_name_status`, this exact scenario reports as
    ``C100  <old>  <new>``, and a genuine, unrelated ``git mv`` elsewhere in
    the SAME diff still reports plain ``R100`` — the two are not confused
    with each other in a mixed batch.

    ★ Corrected in review (lead-coder, reproduced against the REAL repo, not
    a design read — see PR #3913): the first version matched ``C`` on
    EITHER side (``parts[1] OR parts[2]``) and didn't exclude
    ``__init__.py``. A brand-new, EMPTY ``tests/<pkg>/__init__.py`` — the
    exact shape a legitimate Stage-1 migration PR creates for every new
    destination package — is trivially "100% similar" to every OTHER
    empty ``__init__.py`` anywhere in the tree (empty files are all
    byte-identical to each other), so ``-C --find-copies-harder`` matched
    it against some UNRELATED empty ``__init__.py`` elsewhere (e.g.
    ``src/reyn/data/pipelines/__init__.py``) and reported ``C100`` — this
    gate rejecting its own legitimate migration operation, the same shape
    #3885 already had to fix once. Two conditions now required together:

    - the SOURCE (old) path specifically must be under ``tests/`` — a
      match whose source is OUTSIDE ``tests/`` (an unrelated empty file
      elsewhere in the repo) is not a real "copied from a tests/ file"
      scenario, just an empty-content coincidence.
    - the destination is NOT an ``__init__.py`` — that shape is already
      legitimately handled by :func:`offending_lines`'s own empty-content
      check (:data:`_INIT_SUFFIX`), which is unaffected by this change."""
    parts = line.split("\t")
    if not parts[0].startswith("C") or len(parts) != 3:
        return False
    old_path, new_path = parts[1], parts[2]
    if new_path.endswith("/" + _INIT_SUFFIX) or new_path == _INIT_SUFFIX:
        return False
    return old_path.startswith("tests/")


def is_tests_deletion(line: str) -> bool:
    """Whether *line* deletes a ``.py`` file somewhere under ``tests/`` —
    paired with :func:`is_new_file_in_tests_subdir` to distinguish "a file
    MOVED (appeared here, disappeared there)" from "a file was simply
    ADDED" (see :func:`gate_is_active`'s second signal)."""
    parts = line.split("\t")
    return parts[0] == "D" and len(parts) == 2 and parts[1].startswith("tests/") and parts[1].endswith(".py")


def has_matching_basename_rewrite_pair(lines: "list[str]") -> bool:
    """Whether the diff contains a new-subdir ``.py`` file (see
    :func:`is_new_file_in_tests_subdir`) and a deleted ``tests/`` ``.py``
    file (see :func:`is_tests_deletion`) that share the SAME basename — the
    fixed, corrected shape of the second activation signal (#3930).

    ★ Corrected in review (lead-coder's #3930, reproduced on the real repo,
    not a design read — see PR #3929's own real CI red): the original
    signal only checked "does a new-subdir file exist ANYWHERE in the diff"
    AND "does a deletion exist ANYWHERE in the diff" as two INDEPENDENT
    booleans, with no requirement they are the SAME transformation. An
    ordinary PR that happens to add one new test file (per the vocabulary
    gate, #3911) AND delete one unrelated obsolete test file (per the
    six-questions ③ criterion, #3923) — both things this session actively
    encourages doing in the SAME PR — satisfies that shape by coincidence.

    ★ Content similarity CANNOT fix this (measured, not assumed): the
    signal exists specifically for a FULLY-rewritten move — zero bytes
    shared between old and new content, so no similarity threshold, however
    low, can distinguish "a deliberate 0%-similar disguised move" from "two
    genuinely unrelated files" — verified directly against real throwaway
    repos: BOTH cases produce independent ``A``/``D`` lines, never a paired
    ``R``/``C``, at any threshold from git's own default down to ``-M1%``.
    Lowering the similarity threshold (the first idea considered) was
    measured and REJECTED for this reason — it cannot work even in
    principle for the 0%-similarity case this signal targets.

    ★ What actually distinguishes them: a real "move" — however much its
    CONTENT changed — overwhelmingly preserves the file's NAME (that is
    close to what "move" means; a rename that ALSO changes the filename is
    unusual enough that Stage 1's own actual practice is a byte-identical
    ``git mv`` with the name unchanged). Two genuinely unrelated files
    essentially never share a basename by coincidence (verified: #3929's
    real diff has ``test_2708_cred_check_chokepoint.py`` deleted and
    ``test_3905_cli_authentication_error_boundary.py`` added — no
    resemblance). So basename equality is the signal: require the new-
    subdir file and the deleted file to share the SAME
    ``Path(...).name``."""
    import posixpath

    new_names = set()
    for line in lines:
        if is_new_file_in_tests_subdir(line):
            new_names.add(posixpath.basename(line.split("\t")[1]))
    if not new_names:
        return False
    for line in lines:
        if is_tests_deletion(line) and posixpath.basename(line.split("\t")[1]) in new_names:
            return True
    return False


def gate_is_active(lines: "list[str]") -> bool:
    """The gate applies ONLY to a PR whose diff shows REAL evidence of a
    tests/ migration — never a label, a branch-name convention, or any
    other DECLARATION of "this is a migration PR" (lead-coder's #3885
    review correction: a self-declared signal is exactly the
    declaration-vs-reality gap this whole audit exists to close — Tier
    labels, "falsify done" claims, hand-edited baselines, all the same
    shape). Three signals, any one activates:

    - :func:`is_tests_rename` — ANY rename (R100 or a lower-similarity
      inexact rename) under ``tests/`` — only pure R100 is ALLOWED, but any
      similarity ACTIVATES the gate (widened for #3909, see the function's
      own docstring).
    - :func:`has_matching_basename_rewrite_pair` — a brand-new ``.py``
      appearing in a ``tests/`` subdirectory while a ``.py`` of the SAME
      BASENAME disappears from somewhere under ``tests/``, which is what a
      FULLY-rewritten "move" (zero bytes shared, so ``-M100%`` detects no
      rename at all) looks like: appear here, disappear there, same name.

      ★ Corrected THREE times in review, every time found by actually
      running the gate rather than by reading the design:
      1. The first version was "new file in a subdirectory" ALONE — which
         activated on this gate's OWN introducing PR (adding a genuinely
         new test file, no deletion anywhere) and made the gate reject
         itself (CI failure, lead-coder's finding).
      2. The second version added "AND a deletion exists somewhere in the
         diff" — but as two INDEPENDENT booleans over the whole line list,
         with no requirement the appearance and disappearance are the SAME
         transformation. #3930 (lead-coder, real CI red on #3929): an
         ordinary PR adding one new test file (per the vocabulary gate,
         #3911) AND deleting one unrelated obsolete test file (per the
         six-questions ③ criterion, #3923) — both things this session
         actively encourages in the SAME PR — satisfied that shape by pure
         coincidence and activated the gate on a PR that was not a
         migration at all.
      3. This version: requires the appeared and disappeared files to
         share a BASENAME — see :func:`has_matching_basename_rewrite_pair`
         for why content similarity cannot fix this (measured: the 0%-
         similarity case this signal targets is, by construction,
         indistinguishable by content from two unrelated files at any
         threshold) and why basename equality is the real distinguishing
         property instead.
    - :func:`is_tests_copy` — a ``C`` status line under ``tests/`` (#3909):
      the file was COPIED to the destination but the ORIGINAL was never
      deleted. This needs no AND-partner the way the subdir-file signal
      does — a copy that keeps the original cannot masquerade as an
      ordinary new-test-addition PR the way a bare "new file appeared"
      can, because ``git`` itself already found a same-content SOURCE
      file elsewhere in the tree (an ordinary new test, by construction,
      shares no content with anything else — see #3909's issue body for
      why a "total .py count is unchanged" invariant was considered and
      rejected: it would also reject the single most common operation in
      this repo, adding an ordinary new test).

    No signal anywhere → this PR isn't touching tests/'s Stage-1 migration
    at all, whatever it claims to be, and an ordinary Q3/Q4 assert-repair PR
    OR a plain new-test-addition PR (like this one) passes through
    untouched."""
    has_rename = any(is_tests_rename(line) for line in lines)
    has_matching_pair = has_matching_basename_rewrite_pair(lines)
    has_copy = any(is_tests_copy(line) for line in lines)
    return has_rename or has_matching_pair or has_copy


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

        if status.startswith("C") and len(parts) == 3:
            # A `C` (copy) match onto an EMPTY __init__.py is the same
            # false-positive :func:`is_tests_copy` excludes from
            # activation (#3913): an empty new __init__.py is trivially
            # "100% similar" to every OTHER empty file anywhere in the
            # tree, so -C --find-copies-harder can match it against an
            # unrelated empty file even when a REAL rename elsewhere in
            # the same diff already activated the gate. Same allowance as
            # the `A`-status empty-__init__.py case above, content
            # verified the same way — not just the path.
            new_path = parts[2]
            if new_path.endswith("/" + _INIT_SUFFIX) or new_path == _INIT_SUFFIX:
                content = blob_at_head(new_path, root)
                if content == b"":
                    continue
            offenders.append(line)
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
