#!/usr/bin/env python3
"""#5028 — every `tests/` file that spawns `sys.executable` must declare
``out_of_process_reyn``/``reyn_console_scripts``, or the class stays open.

## The incident

A test spawning `sys.executable -c "..."` (or as an MCP stdio `command`)
gets none of pytest's own `[tool.pytest.ini_options] pythonpath = ["src"]`
favour — that setting only puts `<rootdir>/src` on the IN-PROCESS
`sys.path`. The subprocess re-resolves `reyn` from the ambient venv, and in
a git worktree (which has no venv of its own) that answer is whatever
checkout the ambient venv's editable `.pth` happens to point at. Six PRs
(#4996/#5000/#5001/#5015/#5018/#5027) cited the resulting failure —
`ImportError: cannot import name '...' from 'reyn...'`, naming a path under
a DIFFERENT checkout entirely — as "a known pre-existing flake" without
anyone noticing the foreign path in the traceback. #5029 fixed the one file
where the incident actually reproduced; #5028 is the general class.

## Why not #5033's shape (disclose, not gate)

#5033 added a `print(..., file=sys.stderr)` line disclosing the in-process
`reyn.__file__` at `pytest_configure`. Reverted by #5037: the disclosure
lives in the MAIN pytest process, which always resolves correctly (pytest's
own `pythonpath` setting sees to that) — the actual divergence only ever
happens in a SPAWNED SUBPROCESS, a different process this disclosure
structurally cannot see. A form that "always shows the same value" is
vacuous. The fix has to be the side that turns RED when different, not the
side that shows a value — this script is that side, and
`tests/conftest.py`'s ``out_of_process_reyn`` fixture (already fail-loud via
``scripts/verify_env_identity.py``, already used by several files before
this gate existed) is the mechanism it enforces adoption of.

## What this script does NOT do

It does not itself measure whether `reyn` resolves correctly — that's
``out_of_process_reyn``'s job, at test-run time, via a real subprocess probe
(``verify_env_identity.py``). This script is a static population scan: does
the test file *declare* the dependency at all. A file that requests the
fixture but never actually uses its returned path incorrectly, or that spawns
something which doesn't touch `reyn` at all, is outside what a text scan can
tell — see "Population imprecision" below.

## Population imprecision (disclosed, not hidden)

The detector is `sys.executable` appearing as a token in the file's text,
which is the SAME needle #5028's own population census used throughout the
issue, for continuity. It over-counts: a file spawning `sys.executable -c
"print('ok')"` — code that never imports `reyn` — matches the population but
gains nothing from the fixture. It also under-counts: a subprocess spawn wrapped behind a
helper function (spawning `sys.executable` inside a `_support/` module, not
as a literal token in the test file itself) would not match — this is the
DANGEROUS direction, since it hides a real gap behind a green gate rather
than merely flagging an irrelevant one. Neither is fixed here; per-file
"does this specific spawn need pinning" is exactly the ~30-years-old
boundary a plain grep cannot draw
(#4006's own lesson, cited in `check_tests_path_literal_reference.py`), and
demanding that judgment be resolved BEFORE adoption would defer the gate
indefinitely — same reasoning as that script's own ratchet, and
`mypy_ratchet.py`'s.

## A ratchet, not a zero baseline

At the time this gate landed, the gap held enough pre-existing files that
requiring all of them migrated before adoption would have deferred the gate
indefinitely (and most are very likely genuinely exempt — see population
imprecision above) — so this is a committed BASELINE (see
``check_subprocess_reyn_pin_baseline.json`` for the current count; it is a
plain JSON array whose length IS the live number, unlike a count typed into
this prose, which would go stale the moment `origin/main` moved without
anyone editing it here), same skeleton as
``check_tests_path_literal_reference.py``: a file in the baseline is
grandfathered; a file newly entering the gap (a NEW test added later that
spawns `sys.executable` without declaring either fixture) is not, and CI
fails immediately. A file leaving the gap (migrated, or deleted) just stops
appearing — nothing needs editing in the baseline for a fix to "count".

Keyed on the FILE, not (file, line) — an unrelated edit elsewhere in the
file must never itself flip the gate red.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "check_subprocess_reyn_pin_baseline.json"

_SPAWN_RE = re.compile(r"\bsys\.executable\b")
_DECLARED_RE = re.compile(r"\bout_of_process_reyn\b|\breyn_console_scripts\b")


def _iter_tests_py(root: Path = _ROOT) -> "list[Path]":
    """Every TRACKED `tests/**/*.py` file — `git ls-files`, not a directory
    walk (see `check_tests_path_literal_reference.py`'s module docstring for
    why: zero exclusion-list maintenance, a gitignored directory is simply
    never in the output).

    Guards the POPULATION here, not `gap_files()`'s offender count (#5482
    lead-coder/architect review): every file adopting the fixture legitimately
    drives the offender count to zero — that is the gate's own goal state and
    must stay green. The scanned population reaching zero is a different
    claim entirely (wrong `cwd`, a missing `.git`, `git ls-files` itself
    failing silently in some future refactor) and can never be true for a
    real checkout of this repo — a guard placed on the offender count instead
    could not tell "everyone migrated" from "the scan saw nothing", which is
    exactly the empty-collection shape `docs/deep-dives/contributing/
    testing.md` names as wearing green's colour."""
    proc = subprocess.run(
        ["git", "ls-files", "tests"], cwd=root, capture_output=True, text=True, check=True,
    )
    files = [
        root / line for line in proc.stdout.splitlines()
        if line.endswith(".py")
    ]
    if not files:
        raise RuntimeError(
            f"git ls-files tests returned zero .py files under {root} — this "
            "gate cannot distinguish that from every tests/ file having been "
            "deleted, so it refuses to report a (vacuously) clean gap rather "
            "than risk reading an empty scan as 'nothing to fix'."
        )
    return files


def gap_files(root: Path = _ROOT) -> "set[str]":
    """Every `tests/**/*.py` file that spawns `sys.executable` and does NOT
    also reference `out_of_process_reyn` / `reyn_console_scripts` anywhere
    in its own text — the gate's entire decision, isolated from CLI/printing
    so it is directly testable."""
    offenders: "set[str]" = set()
    for path in _iter_tests_py(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _SPAWN_RE.search(text) and not _DECLARED_RE.search(text):
            offenders.add(str(path.relative_to(root)))
    return offenders


def load_baseline(path: Path = _BASELINE_PATH) -> "set[str]":
    return set(json.loads(path.read_text(encoding="utf-8")))


def write_baseline(files: "set[str]", path: Path = _BASELINE_PATH) -> None:
    path.write_text(json.dumps(sorted(files), indent=2) + "\n", encoding="utf-8")


def new_files(measured: "set[str]", baseline: "set[str]") -> "set[str]":
    """The ratchet check itself: any measured file the baseline does not
    already declare is new — a file leaving the gap (migrated, or deleted)
    is not reported here at all, by design (see module docstring)."""
    return measured - baseline


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from a fresh scan instead of checking "
            "against it. Use ONLY after actually migrating/triaging a file "
            "as genuinely exempt — regenerating to silence a new failure "
            "defeats the ratchet (see module docstring)."
        ),
    )
    args = parser.parse_args(argv)

    measured = gap_files(_ROOT)

    if args.write_baseline:
        write_baseline(measured)
        print(f"Wrote {len(measured)} file(s) to {_BASELINE_PATH}")
        return 0

    baseline = load_baseline()
    new = new_files(measured, baseline)

    if not new:
        print(
            f"subprocess-reyn-pin ratchet OK: {len(measured)} file(s) in the gap, "
            f"all baselined ({len(baseline)} declared)."
        )
        return 0

    print("subprocess-reyn-pin ratchet FAILED:\n", file=sys.stderr)
    print(
        f"{len(new)} new file(s) spawn `sys.executable` without declaring "
        f"`out_of_process_reyn`/`reyn_console_scripts` ({_BASELINE_PATH.relative_to(_ROOT)}):",
        file=sys.stderr,
    )
    for file in sorted(new):
        print(f"  {file}", file=sys.stderr)
    print(
        "\nIf this subprocess imports `reyn` (directly, via an MCP stdio "
        "server script, or a `scripts/` entry point): request "
        "`out_of_process_reyn` and pin its returned path as the spawn's "
        "`PYTHONPATH` (see tests/conftest.py's fixture docstring, and "
        "tests/interfaces/test_textual_chat_phase1_3273.py for a worked "
        "example). If it runs a `[project.scripts]` console script by "
        "name: request `reyn_console_scripts` instead. If the spawn "
        "genuinely never touches `reyn` (e.g. `sys.executable -c "
        "\"print('ok')\"`), that's fine too — but say so and regenerate "
        "the baseline (--write-baseline) rather than leaving it "
        "unexplained.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
