"""Tier 1: #5419 — the BEHIND-files discriminant (two-dot minus three-dot
file sets) and its report formatting.

Pins the corrected framing from PR #5420's review (architect +
lead-coder, 2026-08-28): a file present in `git diff base head`
(two-dot) but absent from `git diff base...head` (three-dot) means the
branch is BEHIND main on that file — NOT that merging reverts it. An
earlier revision of this suite asserted "revert" language directly;
that assertion is gone because the claim itself was false (see
`scripts/detect_5419_behind_files.py`'s module docstring, "Corrected
framing").
"""
from __future__ import annotations

import subprocess

from scripts.detect_5419_behind_files import behind_files, format_report


def test_a_file_only_in_two_dot_is_behind():
    """Tier 1: the core set-difference rule."""
    two_dot = ["a.py", "b.py", "c.py"]
    three_dot = ["b.py", "c.py"]
    assert behind_files(two_dot, three_dot) == ["a.py"]


def test_a_file_the_branch_itself_touched_is_not_behind():
    """Tier 1: a file in BOTH sets (the branch's own change, which also
    happens to differ from base for the same reason) must not be
    flagged — only two-dot-ONLY files are behind."""
    two_dot = ["own_change.py"]
    three_dot = ["own_change.py"]
    assert behind_files(two_dot, three_dot) == []


def test_empty_two_dot_yields_nothing_behind():
    """Tier 1: base and head identical — nothing to report."""
    assert behind_files([], []) == []


def test_result_is_sorted_and_deduped():
    """Tier 1: output order/dupes must not depend on git's own listing
    order (never pin algorithm-level behaviour of an external tool)."""
    two_dot = ["z.py", "a.py", "a.py"]
    three_dot = []
    assert behind_files(two_dot, three_dot) == ["a.py", "z.py"]


def test_report_names_every_file_not_just_a_count():
    """Tier 1: #5419 acceptance point ① (lead-coder, citing #4357's
    measured finding that a bare count moved nobody to act) — the report
    text must contain each filename, not a summary count."""
    report = format_report("PR #123", ["docs/x.md", "src/y.py"])
    assert "docs/x.md" in report
    assert "src/y.py" in report


def test_report_is_ok_when_nothing_behind():
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


def test_report_does_not_claim_a_revert():
    """Tier 1: pins the correction from PR #5420's review — the report
    must not claim merging reverts anything (the false claim an earlier
    revision made, which drove real, wasted `update-branch` cycles on
    5 real PRs the same night). It must instead say the opposite:
    merging will NOT revert the file."""
    report = format_report("PR #123", ["x.py"])
    assert "will not revert" in report.lower()


# ---------------------------------------------------------------------------
# Non-vacuity positive control — CONSTRUCTED, not read from a fixed
# historical SHA pair (lead-coder's #5420 review: the prior revision's
# real-SHA fixture is skipped unconditionally in CI, whose pytest job
# checks out shallow — `fetch-depth` unset ⇒ depth 1 — so those SHAs are
# never present; it also rots the moment the source branch is deleted).
#
# Shape built here, fresh every run, in a disposable `tmp_path` repo:
#   1. `base`   commit — creates `shared.py` (both sides will agree on it)
#   2. `branch` commit — off `base`, adds `own.py` (the branch's own,
#      genuine change — must NOT show up as "behind")
#   3. `base` advances again — modifies `shared.py` (main moving a file
#      the branch never touched — THIS is the behind-file)
#
# `shared.py` must land in two-dot (base-now vs branch) but not
# three-dot (base-at-fork vs branch) — exactly the shape a real BEHIND
# branch produces, without depending on any specific commit in this
# repo's own history ever existing or staying reachable.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _build_behind_fixture(repo_dir: str) -> "tuple[str, str]":
    """Returns (base_ref, head_ref) for the constructed BEHIND repo
    described above."""
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "test@example.com", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)

    with open(f"{repo_dir}/shared.py", "w") as f:
        f.write("VALUE = 1\n")
    _git("add", "shared.py", cwd=repo_dir)
    _git("commit", "-q", "-m", "base: add shared.py", cwd=repo_dir)
    fork_point = _git("rev-parse", "HEAD", cwd=repo_dir)

    _git("checkout", "-q", "-b", "feature", cwd=repo_dir)
    with open(f"{repo_dir}/own.py", "w") as f:
        f.write("MINE = True\n")
    _git("add", "own.py", cwd=repo_dir)
    _git("commit", "-q", "-m", "feature: add own.py", cwd=repo_dir)
    head_ref = _git("rev-parse", "HEAD", cwd=repo_dir)

    _git("checkout", "-q", "main" if _has_branch(repo_dir, "main") else "master", cwd=repo_dir)
    with open(f"{repo_dir}/shared.py", "w") as f:
        f.write("VALUE = 2\n")
    _git("commit", "-q", "-am", "base: advance shared.py after the fork", cwd=repo_dir)
    base_ref = _git("rev-parse", "HEAD", cwd=repo_dir)

    assert fork_point  # the fork point itself is not asserted on directly; kept for readability
    return base_ref, head_ref


def _has_branch(repo_dir: str, name: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", name], cwd=repo_dir, capture_output=True,
    )
    return result.returncode == 0


def test_positive_control_constructed_fresh_every_run(tmp_path):
    """Tier 1: non-vacuity, built fresh in a disposable repo on every
    test run (never skipped in CI, never depends on an external SHA
    staying reachable). A gate wired to always compare a ref to itself
    would read an empty, permanently-green 0 here instead."""
    repo_dir = str(tmp_path)
    base_ref, head_ref = _build_behind_fixture(repo_dir)

    two_dot = subprocess.run(
        ["git", "diff", base_ref, head_ref, "--name-only"],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    three_dot = subprocess.run(
        ["git", "diff", f"{base_ref}...{head_ref}", "--name-only"],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    ).stdout.splitlines()

    behind = behind_files(two_dot, three_dot)
    assert behind == ["shared.py"]
