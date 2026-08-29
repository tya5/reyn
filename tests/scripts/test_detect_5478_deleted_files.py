"""Tier 1: #5478 ⑤ — the deleted-files discriminant (three-dot
`--diff-filter=D`) and its report formatting.

Mirrors `tests/scripts/test_detect_5419_behind_files.py`'s own shape
(same report contract, same non-vacuity discipline) — see
`scripts/detect_5478_deleted_files.py`'s module docstring for why
three-dot is the right comparison and why this is deliberately
report-only, never a block.
"""
from __future__ import annotations

import subprocess

from scripts.detect_5478_deleted_files import deleted_files, format_report


def test_a_deleted_file_is_reported():
    """Tier 1: the core pass-through — a file `--diff-filter=D` named is
    in the report."""
    assert deleted_files(["gone.py"]) == ["gone.py"]


def test_no_deletions_yields_nothing():
    """Tier 1: an empty diff-filter=D list reports nothing."""
    assert deleted_files([]) == []


def test_result_is_sorted_and_deduped():
    """Tier 1: output order/dupes must not depend on git's own listing
    order (never pin algorithm-level behaviour of an external tool)."""
    assert deleted_files(["z.py", "a.py", "a.py"]) == ["a.py", "z.py"]


def test_report_names_every_file_not_just_a_count():
    """Tier 1: same #5419 §① rationale this gate reuses — the report
    text must contain each filename, not a summary count."""
    report = format_report("PR #123", ["docs/x.md", "src/y.py"])
    assert "docs/x.md" in report
    assert "src/y.py" in report


def test_report_is_ok_when_nothing_deleted():
    """Tier 1: the clean case reads as "OK", not a suppressed/empty
    report — a silent-0 read must not look identical to "did not run"."""
    report = format_report("PR #123", [])
    assert "OK" in report
    assert "REPORT" not in report


def test_report_never_block_language():
    """Tier 1: #5478 — lead-coder's own "見えることが要点" ruling. The
    report text must read as informational, explicitly disclaiming a
    merge block."""
    report = format_report("PR #123", ["x.py"])
    assert "non-blocking" in report


def test_report_does_not_moralize_the_deletion():
    """Tier 1: #5478 — "削除が在ること自体は悪ではありません" (lead-coder,
    verbatim). The report must not read as an accusation — pins the
    absence of judgment-laden language a future edit could introduce."""
    report = format_report("PR #123", ["x.py"])
    assert "not a problem" in report or "not itself a problem" in report


# ---------------------------------------------------------------------------
# Non-vacuity positive control — CONSTRUCTED, not read from a fixed
# historical SHA pair (same #5419/#5420 discipline this gate reuses: a
# fixture built fresh in a disposable tmp_path repo never skips in CI
# and never rots when a real branch is deleted).
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _has_branch(repo_dir: str, name: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", name], cwd=repo_dir, capture_output=True,
    )
    return result.returncode == 0


def _build_deletion_fixture(repo_dir: str) -> "tuple[str, str]":
    """Returns (base_ref, head_ref): base has `keep.py` + `gone.py`; the
    branch deletes `gone.py` (its own genuine change) and base advances
    `keep.py` afterward (a base-side edit the branch never touches, so
    `keep.py` must NOT show up as deleted from the branch's three-dot
    diff — only `gone.py` should)."""
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "test@example.com", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)

    with open(f"{repo_dir}/keep.py", "w") as f:
        f.write("KEEP = 1\n")
    with open(f"{repo_dir}/gone.py", "w") as f:
        f.write("GONE = 1\n")
    _git("add", "keep.py", "gone.py", cwd=repo_dir)
    _git("commit", "-q", "-m", "base: add keep.py and gone.py", cwd=repo_dir)

    _git("checkout", "-q", "-b", "feature", cwd=repo_dir)
    _git("rm", "-q", "gone.py", cwd=repo_dir)
    _git("commit", "-q", "-m", "feature: delete gone.py", cwd=repo_dir)
    head_ref = _git("rev-parse", "HEAD", cwd=repo_dir)

    _git("checkout", "-q", "main" if _has_branch(repo_dir, "main") else "master", cwd=repo_dir)
    with open(f"{repo_dir}/keep.py", "w") as f:
        f.write("KEEP = 2\n")
    _git("commit", "-q", "-am", "base: advance keep.py after the fork", cwd=repo_dir)
    base_ref = _git("rev-parse", "HEAD", cwd=repo_dir)

    return base_ref, head_ref


def test_positive_control_constructed_fresh_every_run(tmp_path):
    """Tier 1: non-vacuity, built fresh in a disposable repo on every
    test run. A gate wired to compare a ref to itself, or that reads
    the wrong diff spec, would read an empty, permanently-green 0 here
    instead of the real deletion the fixture constructs."""
    repo_dir = str(tmp_path)
    base_ref, head_ref = _build_deletion_fixture(repo_dir)

    three_dot_d = subprocess.run(
        ["git", "diff", f"{base_ref}...{head_ref}", "--diff-filter=D", "--name-only"],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    ).stdout.splitlines()

    deleted = deleted_files(three_dot_d)
    assert deleted == ["gone.py"]
