"""Tier 1: #5419 — the BEHIND-revert-candidate discriminant (two-dot minus
three-dot file sets) and its report formatting.

Pins architect's discriminant (issue #5419 §2): a file present in
`git diff base head` (two-dot) but absent from `git diff base...head`
(three-dot) is one the branch never touched itself, yet differs from
base — a candidate for a silent revert on merge.
"""
from __future__ import annotations

import subprocess

import pytest

from scripts.detect_5419_behind_revert_candidates import (
    behind_revert_candidates,
    format_report,
)


def test_a_file_only_in_two_dot_is_a_candidate():
    """Tier 1: the core set-difference rule."""
    two_dot = ["a.py", "b.py", "c.py"]
    three_dot = ["b.py", "c.py"]
    assert behind_revert_candidates(two_dot, three_dot) == ["a.py"]


def test_a_file_the_branch_itself_touched_is_not_a_candidate():
    """Tier 1: a file in BOTH sets (the branch's own change, which also
    happens to differ from base for the same reason) must not be
    flagged — only two-dot-ONLY files are candidates."""
    two_dot = ["own_change.py"]
    three_dot = ["own_change.py"]
    assert behind_revert_candidates(two_dot, three_dot) == []


def test_empty_two_dot_yields_no_candidates():
    """Tier 1: base and head identical — nothing to report."""
    assert behind_revert_candidates([], []) == []


def test_result_is_sorted_and_deduped():
    """Tier 1: output order/dupes must not depend on git's own listing
    order (never pin algorithm-level behaviour of an external tool)."""
    two_dot = ["z.py", "a.py", "a.py"]
    three_dot = []
    assert behind_revert_candidates(two_dot, three_dot) == ["a.py", "z.py"]


def test_report_names_every_file_not_just_a_count():
    """Tier 1: #5419 acceptance point ① (lead-coder, citing #4357's
    measured finding that a bare count moved nobody to act) — the report
    text must contain each filename, not a summary count."""
    report = format_report("PR #123", ["docs/x.md", "src/y.py"])
    assert "docs/x.md" in report
    assert "src/y.py" in report


def test_report_is_ok_when_no_candidates():
    """Tier 1: the clean case reads as "OK", not as a suppressed/empty
    report — a silent-0 read must not look identical to "did not run"."""
    report = format_report("PR #123", [])
    assert "OK" in report
    assert "REPORT" not in report


def test_report_never_block_language():
    """Tier 1: architect's #5419 §3 ruling — the report text itself must
    read as informational, explicitly disclaiming a merge block, never
    silent about that distinction."""
    report = format_report("PR #123", ["x.py"])
    assert "non-blocking" in report
    assert "not a block" in report


# ---------------------------------------------------------------------------
# Non-vacuity positive control — a REAL commit pair from tonight's own
# incidents (#5419 lead-coder acceptance point ②: verify against a real
# one of the 4 before trusting a "0" result from a synthetic fixture).
#
# base  = a965d8e73  (main, right after #5413 merged — a doc-only PR
#                      touching docs/deep-dives/contributing/verification-hazards.md)
# head  = 493bf70cd8 (a real #5415 PR head, captured live on 2026-08-28
#                      while its mergeStateStatus was BEHIND)
#
# Hand-verified (2026-08-28, this session):
#   git diff a965d8e73 493bf70cd8aa79bde269aad9e421eb57c4e8cfa1 --name-only
#     includes docs/deep-dives/contributing/verification-hazards.md
#   git diff a965d8e73...493bf70cd8aa79bde269aad9e421eb57c4e8cfa1 --name-only
#     does not.
# Both commits are reachable in this repo's object store via prior
# fetches this session performed against origin — `pytest.mark.skipif`
# guards the (should-not-happen-in-CI, but possible in a shallow/GC'd
# clone) case where they are not, rather than a hard fail that would
# hide the real detector tests above.
# ---------------------------------------------------------------------------

_BASE = "a965d8e73"
_HEAD = "493bf70cd8aa79bde269aad9e421eb57c4e8cfa1"
_EXPECTED_FILE = "docs/deep-dives/contributing/verification-hazards.md"


def _commits_available() -> bool:
    for ref in (_BASE, _HEAD):
        if subprocess.run(
            ["git", "cat-file", "-e", ref], capture_output=True,
        ).returncode != 0:
            return False
    return True


@pytest.mark.repo_root_cwd(
    reason="runs `git diff` against real commit SHAs from this repo's own "
    "object store (a positive-control fixture, not a per-test tmp fixture) "
    "— needs cwd == the real repo root's git worktree, not the #3705 "
    "autouse isolated tmp_path (which is not a git repo at all)."
)
def test_positive_control_against_a_real_tonight_incident():
    """Tier 1: non-vacuity — replaying the REAL #5415-vs-#5413 BEHIND
    state (not a synthetic fixture) must yield exactly the file main
    actually advanced and the branch had not caught up on. A gate wired
    to always compare a ref to itself (or an unfetched base) would read
    an empty, permanently-green 0 here instead."""
    if not _commits_available():
        pytest.skip(
            "positive-control commits not present in this clone's object "
            "store (shallow/GC'd checkout) — see module docstring"
        )
    two_dot = subprocess.run(
        ["git", "diff", f"{_BASE}", f"{_HEAD}", "--name-only"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    three_dot = subprocess.run(
        ["git", "diff", f"{_BASE}...{_HEAD}", "--name-only"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    candidates = behind_revert_candidates(two_dot, three_dot)
    assert candidates == [_EXPECTED_FILE]
