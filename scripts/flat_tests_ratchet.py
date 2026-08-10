#!/usr/bin/env python3
"""A file directly in ``tests/`` is a ratchet, not a whitelist — #3879 Stage 0.

INVARIANT: no NEW ``.py`` file lands as a direct child of ``tests/`` (no
subdirectory). ~1,128 files already live there — a gate that "allows
existing, rejects new" needs a name-list of "existing" as its baseline, and
that name-list is the whole design (architect's #3879 comments
5228613324/5228618772; lead-coder's follow-up correction on the design
itself, both linked from issue #3879).

The obvious shape for that name-list — an appendable allowlist ("add your
file's name to let it in") — does not remove the default it is meant to
close: "place flat" just becomes "place flat AND add a line," and the
self-reinforcing structure (today's 1,128 flat files exist because nothing
ever stopped the next one) survives unchanged. So this is a **ratchet**, the
same skeleton as ``mypy_ratchet.py`` (#3726) and
``tests/interfaces/test_3595_s4_slash_handler_seam.py``'s ``_SESSION_RESIDUE``: a
committed BASELINE of every flat filename the tree carries TODAY. A flat
file not in the baseline is new — CI fails, immediately, the same day it
lands. A baselined name that stops appearing (Stage 1's ``git mv`` moving it
into a subdirectory) just silently drops out of ``measured``; nothing has to
be edited to let a move "count."

Regenerating the baseline wholesale to make a new red disappear is the one
way to defeat a ratchet (``mypy_ratchet.py``'s own docstring names this
exactly) — ``--write-baseline`` exists here for the same legitimate uses
(initial adoption, or after Stage 1 shrinks it for real) and carries the
same warning.

One requirement this gate has that ``mypy_ratchet.py`` does not: for mypy,
"the baseline grew" is impossible to mean anything BUT "someone hand-added a
finding," because the baseline is machine-measured findings, never a name a
human chooses. Here the baseline IS a set of names a human could type
directly into the JSON to pre-authorize a new flat file before adding it —
so growth of the baseline itself, independent of whether ``measured`` still
matches it, is the exact shape of defeating the ratchet by hand and must be
its own CI-rejected condition (``--check-growth``, diffed against a base
ref). mypy's ratchet does not need this because a hand-edited baseline entry
there is indistinguishable from a real, still-measured finding; a hand-edited
entry here can exist with NO corresponding file on disk at all.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "flat_tests_baseline.json"
_TESTS_DIR = _ROOT / "tests"


def measured_flat_files(tests_dir: Path = _TESTS_DIR) -> "set[str]":
    """Every ``.py`` file that is a DIRECT child of ``tests_dir`` right now —
    ``tests_dir.glob("*.py")`` does not recurse, so a file already moved into
    a subdirectory is correctly absent.

    Three exclusions, none of them a test file this ratchet's INVARIANT (new
    flat TESTS) is about:

    - ``__init__.py`` — a package marker (#4001 — makes ``tests/`` a real
      package so pytest's import-mode walk never stops at ``tests/`` itself,
      closing the tests/<bucket>/ name-collision class), not a test file.
      The same class of exclusion ``check_migration_diff_shape.py`` already
      grants an empty ``tests/<pkg>/__init__.py`` bucket-marker addition.
    - ``conftest.py`` — pytest fixture configuration, structurally pinned
      flat forever (the PR-workflow rule 4/CLAUDE.md hard-rule docs
      themselves note it as "the one structural exception" for
      ``__file__``-depth references); #3879's own bucket-migration arc has
      never proposed moving it, so it is not a member of the population a
      disposition decision is owed for.
    - a name starting with ``_`` — a shared test-support HELPER
      (``tests/_async_wait.py``), not a test module pytest collects; the
      leading underscore is the same "not a public/collectible thing"
      convention Python itself uses, and lead-coder's #4072 review named
      this and ``conftest.py`` as the two remaining #3879-population
      members that should never have been counted as flat TESTS needing a
      bucket decision in the first place."""
    return {
        p.name
        for p in tests_dir.glob("*.py")
        if p.name != "__init__.py" and p.name != "conftest.py" and not p.name.startswith("_")
    }


def load_baseline(path: Path = _BASELINE_PATH) -> "set[str]":
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data)


def write_baseline(names: "set[str]", path: Path = _BASELINE_PATH) -> None:
    path.write_text(json.dumps(sorted(names), indent=2) + "\n", encoding="utf-8")


def new_flat_files(measured: "set[str]", baseline: "set[str]") -> "set[str]":
    """The ratchet check itself: any measured name the baseline does not
    already declare is new — a name leaving `measured` (a Stage-1 `git mv`)
    is not reported here at all, by design (see module docstring)."""
    return measured - baseline


def baseline_at_ref(
    ref: str, path: Path = _BASELINE_PATH, root: Path = _ROOT,
) -> "set[str] | None":
    """The baseline's content as committed at *ref*, or ``None`` if the ref
    lacks the file entirely (e.g. this gate's own introducing commit) — that
    case is not growth, there is nothing to grow FROM.

    *root* is the git repo *path* is resolved and the ``git show`` runs
    against — defaults to this checkout, but is a real parameter (not a
    hardcoded module constant) so this function is testable against a real,
    throwaway git repo rather than only ever against the live repo it ships
    in."""
    rel = path.relative_to(root)
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel.as_posix()}"],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return set(json.loads(proc.stdout))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from the CURRENT flat-file set instead of "
            "checking against it. Use ONLY for initial adoption or after a "
            "real Stage-1 git-mv shrink — regenerating to silence a new file "
            "landing flat defeats the ratchet (see module docstring)."
        ),
    )
    parser.add_argument(
        "--check-growth",
        metavar="BASE_REF",
        help=(
            "additionally reject if the committed baseline itself grew versus "
            "BASE_REF (e.g. origin/main) — closes the hand-edit-the-baseline "
            "loophole a plain ratchet check does not, see module docstring. "
            "Intended for CI; skipped locally by default (no base ref to diff "
            "against without one)."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    # Read the module globals by NAME here (not via the callees' own default
    # parameter values, bound once at def-time) so a test can monkeypatch
    # `_TESTS_DIR`/`_BASELINE_PATH`/`_ROOT` on this module and have main()
    # actually observe it — a default-arg is evaluated once at function
    # definition, a name lookup inside a function body is not.
    measured = measured_flat_files(_TESTS_DIR)

    if args.write_baseline:
        write_baseline(measured, _BASELINE_PATH)
        print(f"Wrote {len(measured)} flat test filenames to {_BASELINE_PATH}")
        return 0

    baseline = load_baseline(_BASELINE_PATH)
    new = new_flat_files(measured, baseline)

    exit_code = 0

    if new:
        exit_code = 1
        print("flat-tests ratchet FAILED:\n", file=sys.stderr)
        print(
            f"{len(new)} new file(s) land directly in tests/, not in the "
            f"baseline ({_BASELINE_PATH.relative_to(_ROOT)}):",
            file=sys.stderr,
        )
        for name in sorted(new):
            print(f"  tests/{name}", file=sys.stderr)
        print(
            "\nThe existing ~1,128 flat files are grandfathered — this only "
            "blocks NEW additions. Pick a subdirectory for the file(s) above "
            "(tests/_support, tests/fixtures, tests/data, tests/scaffold if "
            "one is not itself a test). `python scripts/suggest_test_dir.py "
            "<path>` prints an advisory suggestion based on the file's "
            "dominant reyn.* import — not enforced, use it or pick something "
            "else.\n\nDo NOT add the name to the baseline to make this pass — "
            "that recreates the exact default this gate exists to remove "
            "(see module docstring). --write-baseline exists only for real "
            "adoption/shrink, never to legitimize a new flat file.",
            file=sys.stderr,
        )

    if args.check_growth:
        old = baseline_at_ref(args.check_growth, _BASELINE_PATH, _ROOT)
        if old is not None and len(baseline) > len(old):
            exit_code = 1
            added = baseline - old
            print(
                f"\nflat-tests ratchet FAILED: the baseline itself grew "
                f"({len(old)} -> {len(baseline)} entries) versus "
                f"{args.check_growth} — the exact loophole a plain ratchet "
                "check does not close: someone can pre-authorize a new flat "
                "file by hand-editing the baseline instead of adding it. "
                f"New baseline entr{'y' if len(added) == 1 else 'ies'}: "
                f"{sorted(added)}",
                file=sys.stderr,
            )

    if exit_code == 0:
        print(
            f"flat-tests ratchet OK: {len(measured)} flat files, all baselined "
            f"({len(baseline)} declared)."
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
