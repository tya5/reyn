#!/usr/bin/env python3
"""#4331 — surface what a green pytest run does NOT say, on the same
screen the green is read on.

## The gap this closes

`tests/interfaces/test_text_effect_cache_3860.py` had 7 `@requires_tte`
arms; CI has no `effects` extra, so the whole file skipped, and green said
nothing about it. Measured (#4331, backlog-watcher): a real CI run carries
**63 skipped / 10801 passed, 0 collection errors** — a real, nonzero
population of tests a green run is silent about, split across skip
reasons nobody had counted before this issue: sandbox-kernel-feature
unavailability (~20, some already covered by a separate CI-only job, some
not — see #4333), macOS-only (~15, #3881's own scope), optional-extra
absence (~16, correct-by-design but never declared as such anywhere in
`docs/`), and everything else (~12).

CLAUDE.md's test-review Q4 names exactly this shape: "skip / collection
error / zero collected all wear green's colour." Q4 blocks on the
SILENCE, not the skip itself — an optional-extra skip is correct design.
What was missing was a place a human reading the green result would
actually see the number.

## Why a doc line was rejected (lead-coder's #4331 ruling)

A doc saying "CI skips ~63 tests because X" goes stale the moment the
population shifts, and — the sharper problem — nobody opens that doc at
the moment they're reading a green check. The fix has to live on the
SAME SURFACE as the green: the job summary GitHub already renders next to
the checks list. This script's whole job is producing that surface's
content, computed fresh from the run that just happened, never a
hardcoded count.

## Not a gate

This script's exit code is always 0 (see `main`'s final `return 0` — a
parse failure degrades to an apologetic summary line, never a nonzero
exit) — #4331's condition 3, explicit: "件数がいくつでも赤にしないでください
… 見えるようにするだけが目的です". A CI step running this must never be
allowed to fail the job over a skip count; skip census is purely
informational, positioned next to the checks a human already reads, not
a new check of its own.

## Population: parsed from the SAME run's own pytest output, not re-run

Takes the log file the actual `pytest -rs` invocation already produced
(piped there via `tee` in the calling workflow step) and parses two
things pytest's own `-r` summary already prints for free:

- Every ``SKIPPED [N] path:line: reason`` line — pytest's own count of
  how many collected test IDs hit that exact skip call site. Summed and
  cross-checked against the final ``"... skipped ..."`` summary line so a
  parsing bug in THIS script is visible (a mismatch prints loudly) rather
  than silently under/over-counting.
- Collection errors — lines pytest emits starting with ``ERROR `` when a
  file fails to import/collect. #4331's condition 2, explicit: skip
  counts do NOT include collection errors (a file that fails to collect
  contributes 0 to the skipped tally, not 1), so a collection-error count
  needs its OWN, separate line on the same summary or that population
  stays invisible even after this script ships.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# pytest's own `-r` short-summary line for a skip:
#   SKIPPED [1] tests/foo.py:123: some reason text
# `loc` (path:line) never contains whitespace; `reason` starts right after
# the first ": " that follows it — matching `\S+` for `loc` is exact, not
# a heuristic, because pytest itself only ever emits this one fixed shape.
_SKIP_LINE_RE = re.compile(r"^SKIPPED \[(\d+)\] (\S+): (.*)$")

# pytest's own final result line, e.g. "10801 passed, 63 skipped, 37
# warnings in 214.11s" or "3 failed, 40 passed, 2 skipped in 1.2s" — used
# only as a CROSS-CHECK against the summed SKIPPED-line counts above, so a
# bug in this script's own parsing surfaces as a printed mismatch instead
# of a silently wrong number.
_TOTAL_SKIPPED_RE = re.compile(r"(\d+) skipped")
_TOTAL_ERRORS_RE = re.compile(r"(\d+) error")

# A collection error: pytest prints a line starting with "ERROR " (not
# indented, not "ERRORS" the section header) naming the file that failed
# to collect. Distinct from a SKIPPED test — a file that fails to collect
# contributes ZERO to the skipped tally, which is exactly why #4331's
# condition 2 requires this be counted and shown separately.
_COLLECTION_ERROR_RE = re.compile(r"^ERROR (\S+)")


def parse_census(log_text: str) -> "dict":
    """Everything the summary needs, computed from one pytest log's text —
    isolated from CLI/output so it is directly testable against a fixture
    string, no subprocess or real pytest run required."""
    skip_reason_counts: "dict[str, int]" = {}
    summed_skips = 0
    for line in log_text.splitlines():
        m = _SKIP_LINE_RE.match(line)
        if m:
            n, _loc, reason = int(m.group(1)), m.group(2), m.group(3)
            skip_reason_counts[reason] = skip_reason_counts.get(reason, 0) + n
            summed_skips += n

    collection_errors = sorted({
        m.group(1) for line in log_text.splitlines()
        if (m := _COLLECTION_ERROR_RE.match(line))
    })

    total_match = _TOTAL_SKIPPED_RE.findall(log_text)
    reported_total_skipped = int(total_match[-1]) if total_match else None

    error_match = _TOTAL_ERRORS_RE.findall(log_text)
    reported_total_errors = int(error_match[-1]) if error_match else None

    return {
        "skip_reason_counts": skip_reason_counts,
        "summed_skips": summed_skips,
        "reported_total_skipped": reported_total_skipped,
        "collection_errors": collection_errors,
        "reported_total_errors": reported_total_errors,
    }


def render_markdown(census: "dict") -> str:
    """The job-summary markdown — every number computed fresh from THIS
    run, nothing hardcoded (#4331 condition 1)."""
    lines = ["## Test-coverage census (#4331 — informational, not a gate)", ""]

    summed = census["summed_skips"]
    reported = census["reported_total_skipped"]
    lines.append(f"**{summed} test(s) skipped** this run.")
    if reported is not None and reported != summed:
        lines.append(
            f"⚠️ parse mismatch: pytest's own summary line reports "
            f"{reported} skipped, but the per-reason SKIPPED lines summed "
            f"to {summed} — this script's parsing may be incomplete "
            "(check for a skip line shape not matched by `_SKIP_LINE_RE`)."
        )
    lines.append("")

    if census["skip_reason_counts"]:
        lines.append("| count | reason |")
        lines.append("|---:|---|")
        for reason, count in sorted(
            census["skip_reason_counts"].items(), key=lambda kv: -kv[1]
        ):
            escaped = reason.replace("|", "\\|")
            lines.append(f"| {count} | {escaped} |")
        lines.append("")

    errors = census["collection_errors"]
    lines.append(f"**Collection errors: {len(errors)}.**")
    if errors:
        lines.append(
            "⚠️ a file that fails to collect contributes 0 to the skipped "
            "tally above — these are invisible in the skip breakdown:"
        )
        for path in errors:
            lines.append(f"- `{path}`")
    else:
        lines.append(
            "(0 is the expected/healthy value — a skip census alone "
            "cannot see a file that failed to collect; this line is what "
            "makes that population visible instead of silently absent.)"
        )

    return "\n".join(lines) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: pytest_skip_census.py <pytest-output-log-path>", file=sys.stderr)
        return 0  # never gate — see module docstring "Not a gate"

    log_path = Path(argv[0])
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        census = parse_census(log_text)
        print(render_markdown(census))
    except Exception as exc:  # noqa: BLE001 — deliberate: never fail the job over this
        print(
            f"## Test-coverage census (#4331)\n\n"
            f"⚠️ census step itself failed to produce a report: {exc!r} "
            "(not a gate — the pytest run above is unaffected).",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
