**[docs-maintainer]** — part of #5314. Design: architect + lead-coder, final spec v3 (issue #5314's own final comment — 5 earlier fragments on that thread are explicitly superseded, per lead-coder's own note).

## What

`scripts/check_open_blocking_checkboxes.py` only read the PR body for an open `- [ ] 🔴` checkbox. Measured, one night (#5311/#5312): deleting the line, or ticking it with no other change, both went green without resolving anything.

**Condition A ∨ condition B, both required**:
- **A (comment)**: a comment whose first line matches `BLOCKING (head <sha>)` is unresolved unless a LATER comment's first line matches `BLOCKING-CLEARED (head <sha>)`, names the PR's **current** head (not stale — a push after the pair reopens it), and whose body contains — verbatim, whitespace-normalized — the BLOCKING comment's own identifying line (its first non-empty line after the marker).
- **B (body, extended from #5135)**: an open `- [ ] 🔴` still fails, unchanged. New: a checked `- [x] 🔴` also fails unless some comment's body contains that line's text verbatim — the same matching rule as A, reused rather than reinvented.

Author is never checked (every session shares one `gh` user — `--json author` can't tell them apart; an identity comparison is always vacuously true, falsified before implementation).

## Designs considered and rejected (falsified before implementation, not just disliked)

- **Author-identity comparison** — always vacuously true, per above.
- **Comment-only detection** (ignore body) — every real blocking point in this repo is raised by editing the PR body (house rule 7); a comment-only gate reads 0 raised points on every PR following the existing convention, closing the deletion bypass by opening a much bigger omission bypass.
- **A count invariant** (BLOCKING count ≤ CLEARED count) — 2 raised points, 2 CLEARED comments both quoting the SAME one, balances the count while the other stays open.

## What this does not buy (disclosed, not hidden — required by the design brief)

**Not authorization.** A BLOCKING comment can still be deleted outright (GitHub allows comment deletion) — this gate has nothing left to require a CLEARED counterpart for if it is. That's a materially bigger, more visible act than editing one's own PR body, and it leaves no residue of even a false claim, but it is not mechanically prevented. What's bought is a **direction**, not an identity check: a body checkbox rewards removal (delete → green); this design punishes it (delete the CLEARED comment that would resolve a BLOCKING comment → the gate has nothing to find → stays red). Closing the remaining gap would mean establishing identity across sessions that share one `gh` user — a separate, more expensive arc this PR does not attempt.

## My own implementation choice, disclosed (the brief didn't pin this)

The BLOCKING comment's identifying text, for matching purposes, is its **first non-empty line after the marker line** — not the whole comment body (too brittle to rewording) and not "any word overlap" (matches almost anything). Same unit condition B already uses for its own checkbox-line matching.

## Flagging, not fixing: a CI-trigger gap this PR does NOT touch

The existing workflow (`check-open-blocking-checkboxes.yml`) only triggers on `pull_request: [opened, edited, synchronize, reopened]` — it does **not** re-run on a new **comment**, so posting a BLOCKING-CLEARED comment would not re-evaluate the gate on its own (the same gap `check-tests-read-names-its-tree.yml` closed for TESTS-READ by adding `issue_comment` + switching to a commit-status report, since an `issue_comment` event's own check run attaches to the default branch's sha, never the PR's).

I looked at mirroring that fix here and reverted it: switching this gate's reporting from a **check run** to a **commit status** changes what shows up in branch-protection's required-checks list by name/mechanism. I don't have visibility into this repo's branch-protection configuration, and getting that wrong risks silently making this gate **not** block merges at all (worse than today) rather than fixing the trigger gap. That's outside `scripts/check_open_blocking_checkboxes.py` (the brief's stated scope) and touches infrastructure with real deploy risk — flagging for lead-coder/architect to decide, not deciding it myself.

## Migration (acceptance condition per the design brief — do not merge before this)

At switch time, `#5311`/`#5313` each have an open blocking point that exists only in the PR body (predates this gate). Per the brief: **lead-coder writes the BLOCKING comment for both before this merges.** I have not merged this PR and will not until that's confirmed.

## Test plan

- [x] `pytest tests/scripts/test_check_open_blocking_checkboxes.py -v` — 12/12 pass (① open-checkbox unchanged, ② checked+verbatim-comment passes, ③ checked+no-comment fails, ④ checked+unrelated-comment fails, ⑤ BLOCKING-only+no-CLEARED fails even with empty body, ⑥ BLOCKING+verbatim CLEARED at current head passes, plus stale-head and differently-worded-CLEARED negative cases).
- [x] `ruff check` on both changed files — clean.
- [x] `python scripts/test_tier_audit.py --strict tests/scripts/test_check_open_blocking_checkboxes.py` — 12/12 OK, 0 Tier-4.
- [x] `python scripts/verify_module_docstrings.py scripts/check_open_blocking_checkboxes.py` — OK.
- [x] `mypy_ratchet.py` / `flat_tests_ratchet.py` / `check_tests_path_literal_reference.py` / `check_bare_tests_import_reference.py` / `check_file_depth_reference.py` — all OK.
- [x] Strip-falsify (3 required): removed condition A entirely → the 4 condition-A tests go RED, the 8 condition-B/plumbing tests unaffected. Removed the checked-line comment requirement → exactly the 2 condition-B-2 tests go RED. Removed the current-head check on CLEARED → exactly the stale-head test goes RED. Each restored, reconfirmed 12/12 green.
- [ ] (skipped — no UI surface, gate/test-only change) Visual

This PR touches `tests/` — needs an independent TESTS-READ (B) before merge (rule 8), from someone other than me. Not self-merging; not arming auto-merge (per the design brief's own explicit instruction, and pending the #5311/#5313 migration above regardless).
