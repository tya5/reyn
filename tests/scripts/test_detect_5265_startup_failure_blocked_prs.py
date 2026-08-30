"""Tier 1: #5265 — the startup_failure-blocked-PR detector's own decision
contract (architect ruling, 2026-08-30).

The 3-case accept set is the ruling's own literal wording:
① no workflow runs at all for a head sha -> flagged (permanently blocked).
② every recorded run is a dead startup_failure(jobs=0) run -> flagged.
③ at least one run is still live/completed normally -> NOT flagged — the
  deny side. Architect's own note: "①②だけの test は『全部名指す』実装でも
  緑になる" (a test covering only ①② would still pass an implementation
  that flags EVERY non-empty runs list, since ① and ② never exercise a
  genuinely-alive run) — ③ is what proves the function discriminates at
  all, not just returns True unconditionally for anything non-empty.

Pure function, no `gh`, no network, no duration (CLAUDE.md's own Ceiling
rule — the whole point of this detector's own design is answering from
structure, never from elapsed time).

Strip-falsify (performed during review): with ``is_permanently_blocked``
reduced to ``return True`` unconditionally, the 4 deny-side tests below
(a live run, a normally-completed run, a mixed dead+live population, and
the jobs!=0 precision case) all go RED; the ①/② accept tests stay green
either way (they never exercised the discrimination). Restored, all 8
pass again.
"""
from __future__ import annotations

from scripts.detect_5265_startup_failure_blocked_prs import (
    format_notification,
    is_permanently_blocked,
)


def test_zero_runs_is_flagged():
    """Tier 1: ① — no workflow run was ever recorded for this head sha."""
    assert is_permanently_blocked([]) is True


def test_all_dead_startup_failure_runs_is_flagged():
    """Tier 1: ② — every recorded run is startup_failure with jobs=0 —
    the exact 28-run shape #5265's own measurement found (28/28,
    run_attempt always 1, GitHub never retried)."""
    runs = [
        {"conclusion": "startup_failure", "jobs_count": 0},
        {"conclusion": "startup_failure", "jobs_count": 0},
    ]
    assert is_permanently_blocked(runs) is True


def test_a_live_in_progress_run_is_not_flagged():
    """Tier 1: ③ — the deny side, and the real content of this test file.
    A run that is still queued/in_progress (no conclusion yet) among the
    recorded runs means required checks MAY still report — must not be
    flagged. Without this test, an implementation that returns True for
    ANY non-empty runs list would still pass ① and ②."""
    runs = [{"conclusion": None, "jobs_count": None}]
    assert is_permanently_blocked(runs) is False


def test_a_normally_completed_run_is_not_flagged():
    """Tier 1: ③'s own sibling — a run that already completed normally
    (success/failure, NOT startup_failure) is not a dead run either;
    required checks from it already reported (or will, on retry) through
    the ordinary path."""
    runs = [{"conclusion": "success", "jobs_count": 12}]
    assert is_permanently_blocked(runs) is False


def test_one_dead_run_alongside_one_live_run_is_not_flagged():
    """Tier 1: mixed population — a startup_failure(jobs=0) run for one
    required workflow alongside a still-running run for another must NOT
    be flagged; at least one path to a reported required check remains."""
    runs = [
        {"conclusion": "startup_failure", "jobs_count": 0},
        {"conclusion": None, "jobs_count": None},
    ]
    assert is_permanently_blocked(runs) is False


def test_a_startup_failure_conclusion_with_nonzero_jobs_is_not_the_signature():
    """Tier 1: deny-side precision — conclusion==startup_failure ALONE is
    not the signature; #5265's own measured shape is jobs==0
    specifically (GitHub accepted the trigger but started nothing). A
    startup_failure with jobs recorded is a different, unmeasured shape
    and must not be conflated with the one this detector targets."""
    runs = [{"conclusion": "startup_failure", "jobs_count": 3}]
    assert is_permanently_blocked(runs) is False


def test_notification_names_the_pr_and_the_recovery_lever_and_its_cost():
    """Tier 1: the notification must name the PR/head AND the recovery
    lever (close/reopen) AND that lever's own known cost (drops
    auto-merge arming) — #5265's own body found this cost the hard way;
    a notification that omits it lets the next reader re-discover it."""
    text = format_notification(5262, "abc123def456789", [])
    assert "#5262" in text
    assert "abc123def456" in text
    assert "close/reopen" in text
    assert "auto-merge" in text.lower()


def test_notification_names_the_specific_reason_for_each_case():
    """Tier 1: the ①/② distinction survives into the notification text —
    a reader should not have to re-derive which of the two structural
    shapes fired from the flag alone."""
    zero_runs_text = format_notification(1, "sha1", [])
    dead_runs_text = format_notification(
        2, "sha2", [{"conclusion": "startup_failure", "jobs_count": 0}],
    )
    assert "no workflow run" in zero_runs_text.lower()
    assert "startup_failure" in dead_runs_text.lower()
