#!/usr/bin/env python3
"""#5265 (recurrence, 2026-09-07, PR #5912) -- detect a PR head sha whose
required branch-protection context will NEVER be reported under its own
NAME, even though nothing on the PR looks red.

## The failure this detects (measured, not guessed -- #5265 issue,
2026-09-07 comment, lead-coder's own direct-execution finding)

`detect_5265_startup_failure_blocked_prs.py` (the original #5265 script)
answers one question: did ANY workflow run start at all for this head?
The 2026-09-07 recurrence (PR #5912) is a DIFFERENT mechanism producing
the SAME terminal symptom -- a required context permanently ABSENT,
never failing -- so a whole-run-absence check does not catch it:

  A CI check suite re-ran on the same head (the `mypy_ratchet` #5882
  matrix-collapse landed the same day and re-triggered CI). The re-run's
  own `pytest` job never expanded its version matrix -- GitHub recorded
  it under the literal, UNEXPANDED template string (`pytest (Python
  ${{ matrix.python-version }})`) instead of concrete per-version names.
  An OLDER run on the SAME head DOES have a real `pytest (Python
  3.11)`/`(3.12)` success -- but GitHub's required-check resolution
  treats the newer run as authoritative for that workflow and never
  falls back to the stale one. Branch protection reported "2 of 7
  required status checks are expected" forever; nothing on the PR's own
  page was red (`gh pr checks` / `statusCheckRollup` / combined status
  were all uniformly SUCCESS -- the FAILURE rows a naive read sees there
  belong to same-named-but-different WORKFLOW JOB rows, not the required
  CONTEXT itself; see this module's own sibling finding in
  ``feedback_statuscheckrollup_mixes_required_context_with_similarly_
  named_workflow_rows`` -- do not repeat that misread here).

## Two required-by-lead-coder detection rules (2026-09-07 comment, verbatim)

  ① Compare branch protection's required-context list against everything
     this head has ANY report under (commit status ∪ check-run), BY
     NAME. A required name with zero reports anywhere is flagged.

  ② A matrix-family required name's staleness is judged by comparing
     ONLY within that family's OWN check-runs (same job-name prefix),
     never against the single highest check_suite id on the WHOLE head.
     Comparing against the head's global max false-positives on every
     healthy PR that happens to have OTHER, unrelated workflows whose
     suites were created later (BLOCKING re-read note, sandbox gates,
     ...) -- lead-coder's own same-day mistake, corrected before this
     script was written.

## Judgement is read-only, always

`gh api -X PUT .../merge` is what actually SURFACED the "2 of 7 required
status checks are expected" text this script's own family-shape check
reproduces read-only -- that PUT call must never be the detection
mechanism: if the PR genuinely IS mergeable the instant you check, it
real-merges it. This script never calls it.

## Verified against real heads (2026-09-07, lead-coder-supplied, 4/4)

  positive: `0ee0072...` (PR #5912, pre-rebase -- WAS permanently blocked
    this way)
  negative: `9c05527...` (#5911), `676e794...` (#5912, post-rebase),
    `37cb926...` (#5921) -- all three healthy.

See ``tests/scripts/test_detect_5265_missing_required_context.py`` for
the fixture-driven 4/4 (captured from these same 4 real heads' `gh api`
output, not synthesized).

## False positive #1 (2026-09-07, production, lead-coder's own
independent reproduction) -- draft PRs

The FIRST live run of the deployed script (BOOTSTRAP.md §5, an actually-
BLOCKED-unchanged PR) fired RED on `#5928`'s draft-time head
(`89494b66f6c1...`). The classification was factually accurate for that
head's shape (the pytest family's newest suite really was the
unexpanded template) -- but the underlying CAUSE was different from
#5265: `#5928` was a DRAFT PR, and draft PRs intentionally never run CI
at all (`test.yml`'s own `draft == false` guard, #4239's own ruling: "a
draft PR is 'not yet asking for CI'"). An absent required context on a
draft is the EXPECTED, visible state, not #5265's silent permanent
block.

Fix: a `--pr` invocation now fetches `isDraft` and short-circuits to OK
without running the classifier at all when true -- the underlying
classification stays exactly as accurate as before (a draft head really
does have the unexpanded-template shape; that fact does not change), the
CAUSE is what determines whether it is #5265's failure mode. A
`--head-sha` invocation cannot check draft status (no PR context) --
a RED verdict from that path now carries an explicit caveat rather than
asserting #5265 with full confidence.

⚠️ The OTHER #5265 mechanism (`startup_failure`/`jobs==0`, no workflow
run at all) has NOT been re-verified against a real head by this PR --
lead-coder does not have a saved head sha for it (that branch's own
script, `detect_5265_startup_failure_blocked_prs.py`, was already
verified against ITS OWN real heads when it landed, #5666/#5665; this
new script does not touch that code path).

## Placement

Same ruling as the original #5265 script (architect, 2026-08-30):
detection only, no automatic recovery; not a GitHub Actions cron
(self-referential silence on exactly the days it is needed); invoked
from OUTSIDE Actions (a peer session's own sweep, or reyn-broker's
`github_pr_watcher.py` plugin -- a different repo, not touched here).

Usage:
    python scripts/detect_5265_missing_required_context.py --pr 5912
    python scripts/detect_5265_missing_required_context.py --head-sha <sha>
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

_REPO = "tya5/reyn"

# The literal, unexpanded GitHub Actions matrix-strategy template marker
# (e.g. `pytest (Python ${{ matrix.python-version }})`) -- present in a
# job name only when the matrix strategy never got to expand it.
_MATRIX_TEMPLATE_MARKER = "${{"

# A required context that is one member of a Python-version matrix looks
# like "<job> (Python 3.11)"; its family key is the "<job> (Python "
# prefix shared with its siblings AND with the unexpanded template name.
_MATRIX_FAMILY_RE = re.compile(r"^(?P<prefix>.+ \(Python )\d+\.\d+\)$")


# ---------------------------------------------------------------------------
# Pure decision logic -- no gh, no network, no duration.
# ---------------------------------------------------------------------------


def reported_context_names(status_contexts: "list[dict]", check_runs: "list[dict]") -> "set[str]":
    """Every name this head has ANY report under. StatusContext entries
    (``{"context": ...}``, the legacy commit-status API) cover names set
    directly via the status API (``open-blocking-checkbox``,
    ``tests-read-names-its-tree`` in this repo); CheckRun entries
    (``{"name": ...}``) cover Actions-produced names (``pytest (Python
    3.11)``, ``ruff``, ...). Neither source alone is complete for this
    repo's actual required-context list -- #5265's 2026-09-07 requirement
    ① is explicitly "commit status ∪ check-run"."""
    names = {s["context"] for s in status_contexts}
    names |= {c["name"] for c in check_runs}
    return names


def find_never_reported(
    required: "list[str]", status_contexts: "list[dict]", check_runs: "list[dict]",
) -> "list[str]":
    """Required-context names with ZERO report anywhere on this head --
    #5265 requirement ①, name-matched against the union of both report
    sources."""
    reported = reported_context_names(status_contexts, check_runs)
    return sorted(r for r in required if r not in reported)


def find_matrix_family_blocked(required: "list[str]", check_runs: "list[dict]") -> "list[str]":
    """Required matrix-family names (``pytest (Python 3.11)``, ...) whose
    OWN family's NEWEST check-run on this head is the unexpanded
    template (``pytest (Python ${{ matrix.python-version }})``) instead
    of a concrete per-version name -- #5265 requirement ②, 2026-09-07
    recurrence. The "newest" comparison is scoped to the family's OWN
    check-runs (same job-name prefix) only -- comparing against the
    head's global max suite id false-positives on any healthy PR with
    unrelated later workflows (accept-side deny case, see the test
    file's own ``deny`` tests)."""
    blocked: "list[str]" = []
    for name in required:
        m = _MATRIX_FAMILY_RE.match(name)
        if not m:
            continue
        prefix = m.group("prefix")
        family_runs = [c for c in check_runs if c["name"].startswith(prefix)]
        if not family_runs:
            continue  # zero reports at all -- find_never_reported's job, not this one
        newest = max(family_runs, key=lambda c: c["suite"])
        if _MATRIX_TEMPLATE_MARKER in newest["name"]:
            blocked.append(name)
    return sorted(set(blocked))


def find_permanently_blocked_required_contexts(
    required: "list[str]", status_contexts: "list[dict]", check_runs: "list[dict]",
) -> "list[str]":
    """The union #5265's 2026-09-07 comment asks for: every required
    context this head will never satisfy under its own name, by either
    mechanism (①: never reported at all; ②: matrix family reversed onto
    an unexpanded newer run). This is the CLASSIFICATION only -- whether
    that classification means "#5265" depends on the PR's draft status,
    which this function has no access to; see ``evaluate``."""
    return sorted(
        set(find_never_reported(required, status_contexts, check_runs))
        | set(find_matrix_family_blocked(required, check_runs)),
    )


def evaluate(
    required: "list[str]", status_contexts: "list[dict]", check_runs: "list[dict]",
    *, is_draft: "bool | None",
) -> "list[str]":
    """The verdict this script actually acts on: ``find_permanently_
    blocked_required_contexts``'s own classification, EXCEPT a known
    draft PR (``is_draft=True``) never blocks -- a draft PR intentionally
    never runs CI (``test.yml``'s own ``draft == false`` guard, #4239's
    own ruling) so an absent required context there is the EXPECTED,
    visible state, not #5265's silent permanent block (2026-09-07
    production false positive, `#5928` -- lead-coder's own independent
    reproduction: the classification was factually accurate, the CAUSE
    was different). ``is_draft=None`` (the ``--head-sha`` path, which has
    no PR context to check) is evaluated the same as a real PR for THIS
    function -- the caller is responsible for attaching an indeterminate
    caveat to a RED verdict in that case (see ``format_notification``'s
    own ``draft_caveat``)."""
    if is_draft:
        return []
    return find_permanently_blocked_required_contexts(required, status_contexts, check_runs)


def format_notification(
    pr_number: "str | int", head_sha: str, blocked_names: "list[str]", *, draft_caveat: bool = False,
) -> str:
    """Names the PR/head AND the specific required-context names stuck --
    a reader must not have to re-run the query themselves to know which
    of the 7 required contexts are the problem. ``draft_caveat`` (the
    ``--head-sha`` path, no PR context available) makes explicit that
    draft status could not be ruled out -- #5928's own false positive
    shape, reproduced with full confidence and no caveat, is what this
    flag exists to never repeat."""
    names_text = ", ".join(blocked_names)
    text = (
        f"RED #5265 -- PR #{pr_number} (head {head_sha[:12]}) is silently "
        f"BLOCKED: required context(s) will never report under their own "
        f"name: {names_text}. Not visible as red anywhere on the PR (`gh "
        "pr checks` / combined status / statusCheckRollup all read clean "
        "-- the FAILURE rows a naive read sees there are same-named "
        "WORKFLOW JOB rows, not the required CONTEXT). Detection is "
        "read-only; do not use `gh api -X PUT .../merge` to check this."
    )
    if draft_caveat:
        text += (
            " ⚠️ Draft status could not be checked (no --pr given, so "
            "no PR context) -- a draft PR intentionally skips CI "
            "(test.yml's draft==false guard, #4239) and would show this "
            "exact shape as a false positive (#5928, 2026-09-07). "
            "Re-run with `--pr <N>` if this head belongs to a known PR."
        )
    return text


# ---------------------------------------------------------------------------
# gh wrapper -- kept separate from the pure logic above. Deliberately
# self-contained (not imported from the sibling #5265 script) so this
# file stays runnable as a plain script path (this repo's own convention,
# see the sibling's own Usage line) without needing `scripts` on
# sys.path as a package.
# ---------------------------------------------------------------------------


def _parse_paginated_json(stdout: str) -> "list[dict]":
    """Split ``gh api``'s stdout into its individual page objects. For an
    OBJECT-shaped response, ``gh api --paginate`` concatenates one
    complete JSON object per page back-to-back on stdout (NOT a JSON
    array) -- see the sibling #5265 script's own #5666 finding, same
    endpoint family. A bare ``json.loads`` only survives a single page."""
    decoder = json.JSONDecoder()
    stdout = stdout.strip()
    pages: "list[dict]" = []
    idx = 0
    while idx < len(stdout):
        obj, end = decoder.raw_decode(stdout, idx)
        pages.append(obj)
        idx = end
        while idx < len(stdout) and stdout[idx].isspace():
            idx += 1
    return pages


def _merge_paginated_pages(pages: "list[dict]", array_key: str) -> dict:
    """Merge *pages* into one dict, cross-checking the server's own
    ``total_count`` against the merged length rather than trusting
    ``--paginate`` finished (#5666 accept ②, same reasoning as the
    sibling script)."""
    merged: "list[dict]" = []
    total_count = None
    for page in pages:
        merged.extend(page.get(array_key, []))
        if "total_count" in page:
            total_count = page["total_count"]
    if total_count is not None and total_count != len(merged):
        raise ValueError(
            f"gh api --paginate returned {len(merged)} {array_key!r} "
            f"entries but total_count={total_count} -- incomplete merge, "
            "refusing to treat this as the full population (#5666)",
        )
    result = dict(pages[0]) if pages else {}
    result[array_key] = merged
    if total_count is not None:
        result["total_count"] = total_count
    return result


def _gh_json(args: "list[str]") -> "list[dict]":
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return _parse_paginated_json(result.stdout)


def _fetch_pr_info(pr_number: str) -> "tuple[str, bool]":
    """head sha AND draft status in one call -- #5928's own false
    positive (2026-09-07) was exactly the gap this closes: a `--pr`
    invocation used to resolve only the head sha and never checked
    whether the PR was a draft at all."""
    pages = _gh_json(["pr", "view", pr_number, "--repo", _REPO, "--json", "headRefOid,isDraft"])
    return pages[0]["headRefOid"], bool(pages[0]["isDraft"])


def _fetch_required_contexts() -> "list[str]":
    pages = _gh_json(["api", f"repos/{_REPO}/branches/main/protection/required_status_checks"])
    return pages[0]["contexts"]


def _fetch_status_contexts(head_sha: str) -> "list[dict]":
    pages = _gh_json(["api", f"repos/{_REPO}/commits/{head_sha}/status", "--paginate"])
    return _merge_paginated_pages(pages, "statuses")["statuses"]


def _fetch_check_runs(head_sha: str) -> "list[dict]":
    pages = _gh_json([
        "api", f"repos/{_REPO}/commits/{head_sha}/check-runs?per_page=100", "--paginate",
    ])
    merged = _merge_paginated_pages(pages, "check_runs")["check_runs"]
    return [{"name": c["name"], "suite": c["check_suite"]["id"]} for c in merged]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect a PR head sha with a required context that will never report by name (#5265, 2026-09-07 recurrence).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", metavar="N", help="PR number -- head sha resolved via `gh pr view`.")
    group.add_argument("--head-sha", metavar="SHA", help="A commit sha directly (no PR lookup).")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    pr_label: "str | int" = "?"
    is_draft: "bool | None" = None
    try:
        if args.pr is not None:
            pr_label = args.pr
            head_sha, is_draft = _fetch_pr_info(args.pr)
        else:
            head_sha = args.head_sha
        required = _fetch_required_contexts()
        status_contexts = _fetch_status_contexts(head_sha)
        check_runs = _fetch_check_runs(head_sha)
    except subprocess.CalledProcessError as exc:
        print(f"gh call failed: {exc.stderr}", file=sys.stderr)
        return 2

    if is_draft:
        print(
            f"OK -- PR #{pr_label} (head {head_sha[:12]}) is a draft; draft PRs "
            "intentionally skip CI (test.yml's draft==false guard, #4239) -- "
            "required-context absence here is the EXPECTED state, not #5265.",
        )
        return 0

    blocked = evaluate(required, status_contexts, check_runs, is_draft=is_draft)
    if blocked:
        print(format_notification(pr_label, head_sha, blocked, draft_caveat=(args.pr is None)))
        return 1

    print(f"OK -- head {head_sha[:12]} has a report (or a live path to one) for all {len(required)} required contexts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
