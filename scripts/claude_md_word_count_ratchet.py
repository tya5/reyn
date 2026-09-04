#!/usr/bin/env python3
"""CLAUDE.md's word count is a ratchet, not a report — #4872.

Every `CLAUDE.md` (root and any nested one, `**/CLAUDE.md`) loads into every
session that reads it — its word count is the cost every rule in it charges
per turn, not a one-time authoring cost. `.github/workflows/claude-md-word-
count.yml` already measured this-head-vs-base per PR, but was, verbatim,
"Report-only: it never fails." Under that gate, root `CLAUDE.md` grew
2,240 -> 2,588 words in 3 weeks (#4869 had just cut 203 words of PROSE with
zero rules removed; #4872 itself moved 255 words of module-scoped rules OUT
to nested files) -- the reduction was overwritten by the next round of
additions before the ink dried. CLAUDE.md's own first cross-cutting
question -- "Who stops this if it repeats?" -- had no answer for this file's
own size: a report has no subject, only a gate does.

Owner ruling (#4872, verbatim): rule COUNT is not the problem ("67 本は上限
に近くはありません") -- word count is. This ratchet does not gate how many
rules exist, only how many words the file(s) carrying them cost.

Same skeleton as `mypy_ratchet.py` (#3726) / `flat_tests_ratchet.py`
(#3879): a committed BASELINE (word count per file, this time, not a set of
names) that only a real PR touching the baseline file can raise. Cross the
baseline -> CI fails, immediately, the same day the growth lands, not
noticed three weeks and 348 words later. A file that shrinks needs no
edit to "count" -- `--write-baseline` exists to lock in a real reduction
the same way `mypy_ratchet.py`'s does for a real fix, but the CHECK itself
never requires it: `measured <= baseline` passes silently regardless of by
how much.

Deliberately does NOT auto-lower the baseline on every green run: a
scripted or CI-side rewrite of a committed file is itself the "silently pay
the cost, or in this case silently BANK the improvement without a PR
reviewing why" shape #4869/#4872's own review vocabulary already treats as
a smell (`--write-baseline` is a human's explicit act, in a real PR, the
same way RAISING the ceiling is).

Raising the ceiling is not forbidden -- CLAUDE.md's own owner ruling here is
explicit that adding rules is fine. What is forbidden is doing so for free:
`--write-baseline` must be run and committed in the SAME PR that grows the
file, which is the concrete form of "write down that you are raising every
session's per-turn cost" (lead-coder's own framing, #4872 dispatch) -- an
action, not merely a comment nobody has to make.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "claude_md_word_count_baseline.json"

#: Directories a `CLAUDE.md` under them would never be a real, load-bearing
#: rules file this ratchet should track -- mirrors the exclusions the
#: existing `claude-md-word-count.yml` workflow's own `git diff` scope
#: already gets for free (only files git tracks) and `flat_tests_ratchet.py`'s
#: own `.venv`/`.git` walk-exclusion precedent.
_EXCLUDED_DIR_NAMES = {".git", ".venv", "node_modules", "__pycache__"}


def _claude_md_files(root: Path = _ROOT) -> "list[Path]":
    """Every `CLAUDE.md` under *root*, sorted for a stable report order --
    the SAME set `.github/workflows/claude-md-word-count.yml`'s own `git
    diff --name-only ... | grep -E '(^|/)CLAUDE\\.md$'` targets, just
    measured directly against the working tree rather than a diff (this
    ratchet checks ABSOLUTE word count, not the per-PR delta that
    report-only workflow already prints)."""
    return sorted(
        p for p in root.rglob("CLAUDE.md")
        if not any(part in _EXCLUDED_DIR_NAMES for part in p.relative_to(root).parts)
    )


def measured_word_counts(root: Path = _ROOT) -> "dict[str, int]":
    """`{relative posix path: word count}` for every `CLAUDE.md` under
    *root* right now. Word count is `str.split()`'s own whitespace-run
    split (verified byte-for-byte against `wc -w` on this repo's own
    CLAUDE.md files, including the CJK/mixed-script prose several of them
    carry) -- native, no `wc` subprocess, no locale dependency."""
    return {
        p.relative_to(root).as_posix(): len(p.read_text(encoding="utf-8").split())
        for p in _claude_md_files(root)
    }


def load_baseline(path: Path = _BASELINE_PATH) -> "dict[str, int]":
    return dict(json.loads(path.read_text(encoding="utf-8")))


def write_baseline(counts: "dict[str, int]", path: Path = _BASELINE_PATH) -> None:
    path.write_text(
        json.dumps(dict(sorted(counts.items())), indent=2) + "\n", encoding="utf-8",
    )


def over_baseline(
    measured: "dict[str, int]", baseline: "dict[str, int]",
) -> "dict[str, tuple[int, int]]":
    """The ratchet check itself: `{path: (baseline, measured)}` for every
    file whose CURRENT word count exceeds its baseline entry, OR that has
    no baseline entry at all (a brand-new `CLAUDE.md` this ratchet has
    never measured is a silent-cost-add of exactly the shape this gate
    exists to catch, the same "new = must be declared" posture
    `flat_tests_ratchet.py` already applies to a new flat test file) --
    baseline 0 for that comparison, so ANY word count on a new file reports
    as growth needing `--write-baseline`, never a free pass.

    A path that DROPS OUT of `measured` (a `CLAUDE.md` deleted or moved) is
    not reported here at all -- nothing to grow FROM once the file is
    gone, mirroring `flat_tests_ratchet.py`'s identical silent-shrink
    treatment for a name leaving its own `measured` set."""
    out: "dict[str, tuple[int, int]]" = {}
    for path, count in measured.items():
        was = baseline.get(path, 0)
        if count > was:
            out[path] = (was, count)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from the CURRENT word counts instead of "
            "checking against it. Use for initial adoption, a real reduction "
            "you want to lock in, or a deliberate, reviewed increase -- "
            "commit the result in the SAME PR that changed the file(s), "
            "never to silently absorb an unreviewed growth."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    measured = measured_word_counts(_ROOT)

    if args.write_baseline:
        write_baseline(measured, _BASELINE_PATH)
        print(f"Wrote {len(measured)} CLAUDE.md word count(s) to {_BASELINE_PATH}")
        return 0

    baseline = load_baseline(_BASELINE_PATH)
    over = over_baseline(measured, baseline)

    if over:
        print("CLAUDE.md word-count ratchet FAILED:\n", file=sys.stderr)
        for path in sorted(over):
            was, now = over[path]
            label = "new file, no baseline entry" if path not in baseline else f"{was} -> {now}"
            print(f"  {path}: {label} ({now - was:+d} words)", file=sys.stderr)
        print(
            "\nEvery CLAUDE.md loads into every session that reads it -- its "
            "word count is a per-turn cost, not a one-time authoring cost. "
            "Either trim the file(s) above back under baseline, or if the "
            "growth is deliberate and reviewed, run `python scripts/"
            "claude_md_word_count_ratchet.py --write-baseline` and commit "
            "the updated baseline in THIS PR -- that is the concrete act of "
            "writing down 'this raises every session's per-turn cost,' not "
            "a comment anyone can skip.",
            file=sys.stderr,
        )
        return 1

    print(
        f"CLAUDE.md word-count ratchet OK: {len(measured)} file(s) measured, "
        f"all at or under baseline ({_BASELINE_PATH.relative_to(_ROOT)})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
