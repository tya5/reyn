"""Tier 1: #5265 (2026-09-07 recurrence, PR #5912) -- the by-NAME
missing-required-context detector's own decision contract (lead-coder's
own verbatim requirements, issue #5265 comment 2026-09-07).

Two rules, each with its own accept/deny pair:

  ① a required context with zero reports anywhere on the head (commit
    status ∪ check-run, matched by name) is flagged.
  ② a matrix-family required context (``pytest (Python 3.11)``) whose
    OWN family's newest check-run is the unexpanded template
    (``pytest (Python ${{ matrix.python-version }})``) is flagged --
    but ONLY when that unexpanded run is the family's own newest; an
    OLDER unexpanded run (a stale artifact from an earlier, superseded
    workflow attempt) must NOT flag a family whose current newest run is
    a real, expanded success. This deny case is the whole point of ②
    scoping "newest" to the family, not the head's global max suite id
    (lead-coder's own same-day mistake, corrected before this script was
    written) -- without it, an implementation that flags any family with
    ANY unexpanded run anywhere in its history would still pass every
    other test here.

The 4 fixtures below (``PYTEST_POSITIVE``/``PYTEST_NEGATIVE_*``) are
``check_runs`` arrays captured verbatim via ``gh api
repos/tya5/reyn/commits/<sha>/check-runs?per_page=100`` against the 4
real heads lead-coder supplied (issue #5265, 2026-09-07 comment) --
trimmed to ``{"name", "suite"}`` (the only fields the pure logic reads),
not synthesized. ``REQUIRED`` is this repo's own real branch-protection
``required_status_checks.contexts`` (captured the same day, `gh api
repos/tya5/reyn/branches/main/protection/required_status_checks`).

Strip-falsify (performed during review): with
``find_matrix_family_blocked`` reduced to "flag every family with ANY
unexpanded-template run in its check_runs, regardless of suite
recency", the accept-side positive test stays green (it has an
unexpanded run) but ``test_an_older_stale_unexpanded_run_does_not_block_a_family_whose_newest_run_is_real``
(the real #9c05527 shape) goes RED -- that deny test is what proves this
detector reads suite recency, not mere presence.
"""
from __future__ import annotations

from scripts.detect_5265_missing_required_context import (
    find_matrix_family_blocked,
    find_never_reported,
    find_permanently_blocked_required_contexts,
    format_notification,
    reported_context_names,
)

REQUIRED = [
    "pytest (Python 3.11)",
    "pytest (Python 3.12)",
    "ruff",
    "test-tier audit",
    "docs build (strict)",
    "open-blocking-checkbox",
    "tests-read-names-its-tree",
]

STATUS_CONTEXTS = [
    {"context": "tests-read-names-its-tree", "state": "success"},
    {"context": "open-blocking-checkbox", "state": "success"},
    {"context": "blocking-has-reread-note", "state": "success"},
]

# PR #5912 pre-rebase (head 0ee0072...) -- real incident: a second CI
# check suite (92325567209) reran on the same head with the pytest job's
# matrix template unexpanded, newer than the real matrix suite
# (92325567037) that has genuine 3.11/3.12 successes.
PYTEST_POSITIVE = [
    {"name": "pytest (Python 3.11)", "suite": 92325567037},
    {"name": "pytest (Python 3.12)", "suite": 92325567037},
    {"name": "ruff", "suite": 92325567037},
    {"name": "test-tier audit", "suite": 92325567037},
    {"name": "docs build (strict)", "suite": 92325567037},
    {"name": "ruff", "suite": 92325567209},
    {"name": "docs build (strict)", "suite": 92325567209},
    {"name": "test-tier audit", "suite": 92325567209},
    {"name": "pytest (Python ${{ matrix.python-version }})", "suite": 92325567209},
]

# PR #5911 (head 9c05527...) -- the DENY shape for ②: this head ALSO has
# an unexpanded-template run (suite 92304802767), but it is OLDER than
# the real matrix suite (92304973971) -- a stale run from an earlier
# attempt, superseded by a real one. Must NOT be flagged.
PYTEST_NEGATIVE_STALE_UNEXPANDED = [
    {"name": "ruff", "suite": 92304973971},
    {"name": "pytest (Python 3.12)", "suite": 92304973971},
    {"name": "pytest (Python 3.11)", "suite": 92304973971},
    {"name": "test-tier audit", "suite": 92304973971},
    {"name": "docs build (strict)", "suite": 92304973971},
    {"name": "test-tier audit", "suite": 92304802767},
    {"name": "docs build (strict)", "suite": 92304802767},
    {"name": "pytest (Python ${{ matrix.python-version }})", "suite": 92304802767},
    {"name": "ruff", "suite": 92304802767},
]

# PR #5912 post-rebase (head 676e794...) -- the simple healthy shape:
# a single suite, no reruns at all.
PYTEST_NEGATIVE_SINGLE_SUITE = [
    {"name": "pytest (Python 3.11)", "suite": 92351273634},
    {"name": "pytest (Python 3.12)", "suite": 92351273634},
    {"name": "ruff", "suite": 92351273634},
    {"name": "docs build (strict)", "suite": 92351273634},
    {"name": "test-tier audit", "suite": 92351273634},
    {"name": "BLOCKING PR carries a re-read note naming its tree", "suite": 92351273635},
]

# PR #5921 (head 37cb926...) -- single suite, plus OTHER unrelated
# workflows on the head with HIGHER suite ids than the pytest suite
# (92353647224 < 92353647708/92353647590/...). This is the exact shape
# that false-positives if "newest" is read as the head's global max
# suite instead of the family's own -- lead-coder's own same-day
# mistake this fixture guards against.
PYTEST_NEGATIVE_LATER_UNRELATED_WORKFLOWS = [
    {"name": "ruff", "suite": 92353647224},
    {"name": "test-tier audit", "suite": 92353647224},
    {"name": "docs build (strict)", "suite": 92353647224},
    {"name": "pytest (Python 3.11)", "suite": 92353647224},
    {"name": "pytest (Python 3.12)", "suite": 92353647224},
    {"name": "sandbox boundary is load-method independent (ld-linux + mmap-load,", "suite": 92353647708},
    {"name": "file-depth-reference gate", "suite": 92353647590},
    {"name": "TESTS-READ note names the tree it read", "suite": 92353647380},
]


# ── ① find_never_reported -- name-matched, commit status ∪ check-run ──


def test_a_required_name_with_zero_reports_anywhere_is_flagged():
    """Tier 1: ① accept -- a required name absent from both status
    contexts and check-runs must be flagged."""
    missing = find_never_reported(
        ["a-context-that-was-never-reported"], STATUS_CONTEXTS, PYTEST_NEGATIVE_SINGLE_SUITE,
    )
    assert missing == ["a-context-that-was-never-reported"]


def test_a_required_name_reported_via_status_api_is_not_flagged():
    """Tier 1: ① deny -- StatusContext-sourced names (set directly via
    the commit-status API, not a workflow job) must count as reported;
    without this, a repo whose required checks include any StatusContext
    (this repo's own ``open-blocking-checkbox``) would always false-RED."""
    missing = find_never_reported(["open-blocking-checkbox"], STATUS_CONTEXTS, [])
    assert missing == []


def test_a_required_name_reported_via_check_run_is_not_flagged():
    """Tier 1: ① deny sibling -- CheckRun-sourced names (Actions jobs)
    must also count; without this, every Actions-produced required
    context would always false-RED."""
    missing = find_never_reported(["ruff"], [], PYTEST_NEGATIVE_SINGLE_SUITE)
    assert missing == []


def test_reported_context_names_is_the_union_of_both_sources():
    """Tier 1: ``reported_context_names`` itself -- #5265's own 2026-09-07
    requirement is explicitly "commit status ∪ check-run"; a union that
    silently dropped one source would only surface as spurious flags on
    whichever required name happens to come from the dropped source."""
    names = reported_context_names(STATUS_CONTEXTS, PYTEST_NEGATIVE_SINGLE_SUITE)
    assert "open-blocking-checkbox" in names  # from status
    assert "ruff" in names  # from check-runs


# ── ② find_matrix_family_blocked -- same-family newest-suite comparison ──


def test_the_real_5912_positive_head_is_flagged():
    """Tier 1: ② accept -- the real #5265 2026-09-07 incident shape
    (verified against the actual head, issue #5265 comment): the
    family's own newest suite is the unexpanded template -> both
    pytest required names flagged."""
    blocked = find_matrix_family_blocked(REQUIRED, PYTEST_POSITIVE)
    assert blocked == ["pytest (Python 3.11)", "pytest (Python 3.12)"]


def test_an_older_stale_unexpanded_run_does_not_block_a_family_whose_newest_run_is_real():
    """Tier 1: ② deny -- THE test that proves this detector reads suite
    RECENCY within the family, not mere presence of an unexpanded run
    anywhere in the family's history (real #9c05527/#5911 shape: an
    older stale unexpanded run coexists with a newer real success).
    Without this test, an implementation that flags any family
    containing ANY unexpanded-template run at all would still pass the
    accept-side test above."""
    blocked = find_matrix_family_blocked(REQUIRED, PYTEST_NEGATIVE_STALE_UNEXPANDED)
    assert blocked == []


def test_a_single_clean_suite_is_not_blocked():
    """Tier 1: ② deny sibling -- the simplest healthy shape (no rerun at
    all, real #676e794/#5912-post-rebase)."""
    blocked = find_matrix_family_blocked(REQUIRED, PYTEST_NEGATIVE_SINGLE_SUITE)
    assert blocked == []


def test_unrelated_later_workflows_on_the_same_head_do_not_false_positive():
    """Tier 1: ② deny -- THE regression guard for lead-coder's own
    same-day mistake (comparing against the head's global max suite id
    instead of the family's own). Real #37cb926/#5921 shape: other,
    unrelated workflows on this head have HIGHER suite ids than the
    pytest family's own suite. A "global max" implementation would read
    the pytest family as stale relative to those unrelated suites and
    false-positive on this healthy head."""
    blocked = find_matrix_family_blocked(REQUIRED, PYTEST_NEGATIVE_LATER_UNRELATED_WORKFLOWS)
    assert blocked == []


def test_a_required_name_not_shaped_like_a_matrix_family_is_never_considered():
    """Tier 1: ② scope -- a required name that doesn't match the
    ``<job> (Python X.Y)`` shape (``ruff``, ``open-blocking-checkbox``)
    is never evaluated by this rule at all (①'s job, not ②'s)."""
    blocked = find_matrix_family_blocked(["ruff", "open-blocking-checkbox"], PYTEST_POSITIVE)
    assert blocked == []


def test_a_family_with_zero_reports_at_all_is_left_to_find_never_reported():
    """Tier 1: ② boundary -- a matrix-family required name with NO
    check-runs under its family prefix at all (not even a template) must
    not be claimed by this rule (it has nothing to compare "newest"
    against) -- that case belongs to ① instead."""
    blocked = find_matrix_family_blocked(REQUIRED, [])
    assert blocked == []
    assert find_never_reported(REQUIRED, [], []) == sorted(REQUIRED)


# ── combined union, against the real 4 heads (4/4) ──


def test_the_real_positive_head_is_flagged_end_to_end():
    """Tier 1: the real #5265 2026-09-07 positive, through the combined
    ``find_permanently_blocked_required_contexts`` entry point."""
    blocked = find_permanently_blocked_required_contexts(REQUIRED, STATUS_CONTEXTS, PYTEST_POSITIVE)
    assert blocked == ["pytest (Python 3.11)", "pytest (Python 3.12)"]


def test_all_three_real_negative_heads_are_clean_end_to_end():
    """Tier 1: the 3 real negative heads lead-coder supplied, through the
    combined entry point -- 3/3 clean, completing the 4/4 lead-coder's
    own dispatch required before opening the PR."""
    for check_runs in (
        PYTEST_NEGATIVE_STALE_UNEXPANDED,
        PYTEST_NEGATIVE_SINGLE_SUITE,
        PYTEST_NEGATIVE_LATER_UNRELATED_WORKFLOWS,
    ):
        assert find_permanently_blocked_required_contexts(REQUIRED, STATUS_CONTEXTS, check_runs) == []


def test_notification_names_the_pr_the_head_and_the_specific_blocked_names():
    """Tier 1: the notification must name which of the required contexts
    are stuck, not just that "something" is -- a reader must not have to
    re-run the query themselves."""
    text = format_notification(5912, "0ee007268fe1bfb366f40b3d598cd74525d5d937", ["pytest (Python 3.11)"])
    assert "#5912" in text
    assert "0ee007268fe1" in text
    assert "pytest (Python 3.11)" in text
    assert "read-only" in text.lower()
