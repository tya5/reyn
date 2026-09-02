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
    _fetch_runs_for_head,
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


# ── #5665: the head-sha fetch itself, no `gh run list --limit N` window ──
#
# lead-coder's own real-machine measurement (2026-09-02, PR #5640): the OLD
# `_fetch_runs_for_head` fetched the repo's own most-recent `--limit 200`
# runs and filtered client-side — a 70-minute WINDOW on a busy day, not a
# complete read. Any head older than the window produced an empty list
# indistinguishable from "GitHub never recorded a run" (the exact #5265
# false-positive shape). These tests exercise `_fetch_runs_for_head` itself
# (not just `is_permanently_blocked`) with a Fake `gh_json_fn` — a real
# collaborator (`gh`/network) is not cheaply constructible in a unit test,
# so a Fake at THIS I/O boundary is the policy-compliant substitute
# (testing.md's own carve-out), never a mock of reyn's own logic.


def test_head_sha_outside_the_old_200_run_window_still_returns_its_runs():
    """Tier 1: #5665 accept — PR #5640's own real shape (22 runs recorded
    for a head sha ~4.5 hours old, well outside the OLD client-side
    window). A Fake `gh_json_fn` returns the SAME
    ``{"total_count": 22, "workflow_runs": [...]}`` payload the real
    `gh api repos/.../actions/runs?head_sha=...` call produced (lead-
    coder's own issue-body measurement) — this must come back non-empty,
    proving the fetch itself no longer depends on any recency window."""
    payload = {
        "total_count": 22,
        "workflow_runs": [
            {"id": 1, "conclusion": "success"},
            {"id": 2, "conclusion": "failure"},
        ],
    }
    calls: "list[list[str]]" = []

    def _fake_gh_json(args: "list[str]") -> object:
        calls.append(args)
        return payload

    runs = _fetch_runs_for_head("2358e1bac2191b4e801c256e93dcaeff2fd3ce71", gh_json_fn=_fake_gh_json)

    assert runs == [
        {"conclusion": "success", "jobs_count": None},
        {"conclusion": "failure", "jobs_count": None},
    ]
    assert not is_permanently_blocked(runs), "22 real runs must not read as permanently blocked"


def test_the_query_filters_server_side_never_gh_run_list_with_a_limit():
    """Tier 1: #5665 — proves the FIX ITSELF, not just its interpretation:
    "increase --limit 200" was explicitly rejected (lead-coder) as the
    same window-shaped bug, only wider. Asserts the constructed `gh`
    invocation uses the head-sha-filtered REST endpoint (`actions/runs?
    head_sha=`) with `--paginate` for a provably-complete read, and never
    `run list`/`--limit` at all — a regression here would silently
    reintroduce a window regardless of how large."""
    calls: "list[list[str]]" = []

    def _fake_gh_json(args: "list[str]") -> object:
        calls.append(args)
        return {"total_count": 0, "workflow_runs": []}

    _fetch_runs_for_head("deadbeef", gh_json_fn=_fake_gh_json)

    # exactly one call was made — a window-shaped fix would loop `gh run
    # list` pages client-side; this unpack itself raises if that regresses.
    (call,) = calls
    assert "run" not in call or "list" not in call, f"must not use `gh run list`: {call}"
    assert not any(a == "--limit" for a in call), f"must not use a --limit window: {call}"
    assert any("head_sha=deadbeef" in a for a in call), f"must filter server-side by head_sha: {call}"
    assert "--paginate" in call, f"must prove completeness via pagination: {call}"


def test_a_head_sha_with_zero_real_runs_is_still_flagged_the_true_5265_case():
    """Tier 1: #5665 deny-side (must not regress) — a head sha with a
    GENUINE zero-run population (the true #5265 incident shape: GitHub
    never recorded a run at all) must still come back empty and still be
    flagged. This is the case the whole detector exists for; the #5665
    fix must not turn every "0" into "unknown" the other way."""
    def _fake_gh_json(_args: "list[str]") -> object:
        return {"total_count": 0, "workflow_runs": []}

    runs = _fetch_runs_for_head("neverstarted", gh_json_fn=_fake_gh_json)

    assert runs == []
    assert is_permanently_blocked(runs) is True


def test_a_head_sha_with_only_dead_startup_failure_runs_is_still_flagged():
    """Tier 1: #5665 deny-side sibling — the true #5265 ② shape (every
    recorded run is startup_failure/jobs=0) survives the new fetch path
    unchanged, including the second `gh api .../jobs` call for job count."""
    def _fake_gh_json(args: "list[str]") -> object:
        if args[0] == "api" and args[1].endswith("/jobs"):
            return {"jobs": []}
        return {
            "total_count": 1,
            "workflow_runs": [{"id": 999, "conclusion": "startup_failure"}],
        }

    runs = _fetch_runs_for_head("deadhead", gh_json_fn=_fake_gh_json)

    assert runs == [{"conclusion": "startup_failure", "jobs_count": 0}]
    assert is_permanently_blocked(runs) is True


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
