#!/usr/bin/env python3
"""A mypy finding is a ratchet, not a report — new ones fail CI, old ones don't need closing first.

#3726: mypy has been configured in ``pyproject.toml`` since before this script
existed, and no CI job ever ran it — a declared check with no execution, the
same shape #3024 (env-identity) and #3595 S4 (the slash residue gate) each hit
independently. Two of the day's real defects (`compact_caps` never wired,
`fv.cursor` reading an attribute 0.12.0 dropped) were exactly the class mypy
exists to catch and neither showed up until someone ran it by hand.

The straightforward fix — wire mypy into CI as a blocking gate — does not
work today: a real measurement against the current tree returns 603 errors.
Requiring that backlog closed before adoption defers adoption indefinitely
(every day it stays unadopted is another day a `compact_caps`-shaped defect
can land unnoticed), and a **report-only** job does not close the gap either
— it would just be a second instance of "configured, running, changing
nobody's decision," a report nobody reads is not meaningfully different from
a check nobody runs.

So this is a **ratchet**, the same skeleton as
``tests/interfaces/test_3595_s4_slash_handler_seam.py``'s ``_SESSION_RESIDUE``: a
committed BASELINE of every ``(file, error-code)`` pair the tree currently
carries. A pair not in the baseline is new — CI fails, immediately, the same
day the defect lands. A pair that WAS in the baseline and is gone because
someone fixed it just silently stops appearing; nothing has to be edited to
let a fix "count." The baseline can only ever be read down over time by
actual fixes — regenerating it wholesale to make a new failure disappear is
the one way to defeat the ratchet, and is exactly the file-count-preserving
excuse #3123's own review vocabulary calls out.

Deliberately keyed on ``(file, error-code)``, not the bare per-file error
COUNT: a count-only baseline lets "fix one, introduce a different one" pass
silently (the fixed count and the new count can net to the same total). Keyed
this way, a genuinely NEW finding in an already-baselined file for a
DIFFERENT code is still visible — only the exact ``(file, code)`` pairs
already declared are grandfathered.

The chosen grain has a known, accepted cost (#3738 review): a genuinely NEW
finding of the SAME code in a file that already carries that code is
INVISIBLE — grandfathered under the existing pair, same as a real fix would
be. Confirmed live: tightening a constructor's type in #3738 introduced a
real new ``[arg-type]`` error in ``session.py``, which already carries an
``[arg-type]`` baseline entry from an unrelated pre-existing finding — the
ratchet stayed green. ``(file, line, code)`` would close this, but line
numbers do not survive an unrelated edit shifting them on a moving ``main``
— regenerating the baseline to follow line drift is indistinguishable from
regenerating it to silence a real new failure, which is the one way this
module's own docstring already names as defeating the ratchet. So the
coarser grain is a deliberate trade, not an oversight: it buys stability
across unrelated edits at the cost of blindness to same-code same-file
recurrences, and the latter needs a human (or a future, sharper gate) to
catch, same as any other grandfathered debt in this baseline.

The 76 ``reyn.config`` re-export false positives (#1682's ``_reexport()``,
invisible to mypy's static analysis, documented in #3726's own triage) stay
IN the baseline rather than being carved out via a per-module mypy
``ignore_errors`` override — an exclude-config is itself a new declaration to
maintain, and the point of this script is to draw one line (the baseline)
before adding more.

#3727 (verification-hazards.md §18 "B. Misidentification"): a ``[syntax]``
finding is not "one more red" the way
``[attr-defined]``/``[arg-type]`` are. mypy hits a fatal parse error and
stops the WHOLE invocation ("errors prevented further checking") — every
OTHER file's findings this run are simply unmeasured, not confirmed clean.
Verified directly: injecting a fresh `` # type: `` -prefixed prose comment
(the #3726/#3728 collision shape) into an otherwise-clean file makes THAT
file the only one mypy reports on, full stop. ``main()`` prints a distinct
warning when a new ``[syntax]`` pair appears, because the red's own SHAPE
carries information a bare pair list does not: fix that one before trusting
anything else this run says.

#4576 is that same hazard one layer earlier: not "the run was truncated" but
"the run never happened." With mypy absent from the interpreter's environment,
``python -m mypy`` writes ``No module named mypy`` to stderr and exits 1 —
which :func:`run_mypy` deliberately does not raise on (mypy's own error exit
is the common case), and which :func:`parse_mypy_output` finds no
``[code]`` lines in. Zero measured pairs minus the baseline is zero new pairs,
so the script printed ``OK: 0 findings, all baselined (215 declared)`` and
exited 0 — the ``215 declared`` making it look fully alive, because loading
the baseline HAD succeeded. Measured directly in a ``python -m venv`` with no
mypy installed; found because #4575's local run was green and CI's was not,
and the real ``[call-arg]`` it had been hiding surfaced the moment mypy
actually ran.

The guard is :func:`mypy_is_importable`, checked BEFORE the run: a structural
question (is the module there) rather than a textual one (does the output look
like a real run). Deliberately not a check on mypy's summary wording — that
would make a third party's output format decide whether we trust our own
measurement, and mypy is free to reword it. The exit code cannot serve either:
a missing module and a normal findings-reported run BOTH exit 1 (measured).
NOT covered, disclosed rather than silently half-closed: mypy installed but
crashing mid-run in some shape the ``[syntax]`` guard above doesn't already
name. That shape has never been observed here; this one had.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "mypy_ratchet_baseline.json"
_TARGET = "src/reyn"

# Matches mypy's normal-mode error line:
#   src/reyn/foo/bar.py:123: error: <message>  [error-code]
# Deliberately excludes `note:` lines (mypy emits explanatory notes attached
# to some errors, e.g. "note: Error code ... not covered by ...") and the
# trailing "Found N errors in M files" summary line — neither carries a
# `[code]` a future run can be diffed against.
_ERROR_LINE = re.compile(r"^(?P<file>[^:]+\.py):\d+: error: .*\[(?P<code>[a-z][a-z0-9-]*)\]\s*$")


def mypy_is_importable() -> bool:
    """Whether ``mypy`` can be imported by the interpreter that will run it.

    The precondition every number this script prints depends on: with mypy
    absent, the measured set is empty for a reason that has nothing to do with
    the code under analysis, and "0 new findings" then means "0 findings were
    looked for" (#4576).
    """
    return importlib.util.find_spec("mypy") is not None


_MYPY_MISSING = (
    "mypy is not importable by {exe} — NOTHING WAS MEASURED. This is not "
    "'0 findings': the baseline may or may not still hold, and this run "
    "cannot tell you which. Install it (`pip install -e \".[dev]\"`) and run "
    "again. Green here without mypy present is the #4576 false green, which "
    "hid a real [call-arg] through a full local pre-PR check."
)


def run_mypy(root: Path = _ROOT, target: str = _TARGET) -> str:
    """Run mypy against ``target`` and return its combined stdout+stderr.

    Never raises on a non-zero exit — mypy exits 1 whenever it reports any
    error, which is the expected, common case this script exists to handle,
    not a failure of the subprocess call itself.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", target],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.stdout + proc.stderr


def parse_mypy_output(text: str) -> "set[tuple[str, str]]":
    """Extract the ``{(file, error-code)}`` set mypy's output reports.

    Multiple error lines in the same file for the same code (mypy repeats a
    `[attr-defined]` finding once per occurrence) collapse to one pair — the
    ratchet is keyed on "does this file still carry this CLASS of finding",
    not on exact line counts (a line shifting because of an unrelated edit
    above it must never itself flip the gate red)."""
    pairs: set[tuple[str, str]] = set()
    for line in text.splitlines():
        m = _ERROR_LINE.match(line)
        if m:
            pairs.add((m.group("file"), m.group("code")))
    return pairs


def load_baseline(path: Path = _BASELINE_PATH) -> "set[tuple[str, str]]":
    data = json.loads(path.read_text(encoding="utf-8"))
    return {(entry["file"], entry["code"]) for entry in data}


def write_baseline(pairs: "set[tuple[str, str]]", path: Path = _BASELINE_PATH) -> None:
    data = [{"file": f, "code": c} for f, c in sorted(pairs)]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def new_findings(
    measured: "set[tuple[str, str]]", baseline: "set[tuple[str, str]]"
) -> "set[tuple[str, str]]":
    """The ratchet check itself: any measured pair the baseline does not
    already declare is new — a pair leaving the measured set (a fix) is not
    reported here at all, by design (see module docstring)."""
    return measured - baseline


def syntax_pairs_in(pairs: "set[tuple[str, str]]") -> "set[tuple[str, str]]":
    """The `[syntax]` subset of ``pairs`` — mypy's signal that it hit a fatal
    parse error and stopped ("errors prevented further checking"), the SAME
    shape #3726/#3728 found at `config/root.py:147`: trusting a truncated
    run as if it were a complete one is a misidentification of what was
    actually measured (docs/deep-dives/contributing/verification-hazards.md
    §18 "B. Misidentification").

    Takes ``measured``, not ``new`` — a `[syntax]` pair that happens to
    already be baselined is NOT "known debt" the way every other code is: a
    baselined `[attr-defined]` means "we've seen this file's finding before
    and haven't fixed it," but a baselined `[syntax]` would mean "the last
    time this ran, mypy checked exactly one file and called it OK," and every
    run after would silently repeat that with no new pair to flag (#3727
    review). `main()` treats ANY `[syntax]` pair in `measured` as fatal,
    baselined or not, and refuses to bake one into the baseline at all via
    `--write-baseline` — the one operation the module docstring already names
    as "the one way to defeat the ratchet" would otherwise defeat THIS
    specific check permanently, silently, in one command."""
    return {p for p in pairs if p[1] == "syntax"}


_SYNTAX_ABORT_WARNING = (
    "[syntax] above is not \"one more red\": a fatal parse error stops "
    "mypy's ENTIRE run, so no other file was actually checked this time — "
    "every OTHER pair this run's output doesn't mention is UNMEASURED, "
    "not confirmed clean. Fix the [syntax] finding first; nothing else "
    "this run says can be trusted until it's gone (verification-hazards.md "
    "§18 \"B. Misidentification\")."
)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from a fresh mypy run instead of checking "
            "against it. Use ONLY after actually fixing/triaging findings — "
            "regenerating to silence a new failure defeats the ratchet (see "
            "module docstring)."
        ),
    )
    args = parser.parse_args(argv)

    # #4576: before anything else — including --write-baseline, which would
    # otherwise overwrite the baseline with the empty measurement of a run
    # that never happened, silently discarding every declared pair.
    if not mypy_is_importable():
        print(_MYPY_MISSING.format(exe=sys.executable), file=sys.stderr)
        return 1

    output = run_mypy()
    measured = parse_mypy_output(output)
    syntax_pairs = syntax_pairs_in(measured)

    if args.write_baseline:
        if syntax_pairs:
            print(
                "REFUSING to write baseline: this measurement contains a "
                "[syntax] finding, which means mypy aborted before checking "
                "most of the tree. Baselining it would bake in a "
                "permanently-degraded run that reports \"OK\" forever while "
                "only ever checking one file. Fix the [syntax] finding(s) "
                "first, then regenerate.\n",
                file=sys.stderr,
            )
            for file, code in sorted(syntax_pairs):
                print(f"  {file}  [{code}]", file=sys.stderr)
            return 1
        write_baseline(measured)
        print(f"Wrote {len(measured)} (file, code) pairs to {_BASELINE_PATH}")
        return 0

    baseline = load_baseline()
    new = new_findings(measured, baseline)

    if not new and not syntax_pairs:
        print(f"mypy ratchet OK: {len(measured)} findings, all baselined ({len(baseline)} declared).")
        return 0

    print("mypy ratchet FAILED:\n", file=sys.stderr)
    if new:
        print(
            f"{len(new)} new (file, error-code) pair(s) not in the baseline "
            f"({_BASELINE_PATH.relative_to(_ROOT)}):",
            file=sys.stderr,
        )
        for file, code in sorted(new):
            print(f"  {file}  [{code}]", file=sys.stderr)
    if syntax_pairs:
        # Fires even when every syntax_pairs entry is already baselined (so
        # absent from `new`) — a baselined [syntax] is never "known debt",
        # see syntax_pairs_in()'s own docstring.
        print(f"\n{_SYNTAX_ABORT_WARNING}", file=sys.stderr)
        for file, code in sorted(syntax_pairs):
            note = "" if (file, code) in new else "  (already \"baselined\" — still fatal, see above)"
            print(f"  {file}  [{code}]{note}", file=sys.stderr)
    print(
        "\nEither fix the new finding(s), or if this is a deliberate, understood "
        "addition, add the (file, code) pair to the baseline explicitly — do "
        "NOT regenerate the whole baseline with --write-baseline to silence this.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
