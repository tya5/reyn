"""Tier 1: #5691 — the test-tier-audit CI step's own `CHANGED` computation
(`.github/workflows/test.yml`'s "Run test-tier audit" step) must EXCLUDE a
purely-deleted test file from its changed-files list, and must still
INCLUDE a genuinely modified one.

## The defect this pins (#5691, lead-coder finding)

`git diff --name-only "$BASE"...HEAD -- 'tests/**.py'` (no `--diff-filter`)
lists a DELETED file too. A PR containing only test deletions (#5690's own
CI failure — real, reproduced) therefore makes `$CHANGED` non-empty, so the
"no test files changed — skip" guard never fires, and
`test_tier_audit.py --strict` is handed a path that no longer exists on
disk — exit 1 with "0 files audited", not the intended skip.

The fix: `--diff-filter=d` (lowercase — EXCLUDES status D, keeps
A/C/M/R/T/U/X/B). Mirrors `tests/scripts/test_detect_5478_deleted_files.py`'s
own technique — a disposable repo built FRESH in `tmp_path` on every run
(never a fixed historical SHA pair, so this can't rot when a real branch is
deleted), running the EXACT git invocation the workflow step uses, not a
re-derived approximation of it.

Two tests, not one (lead-coder's own explicit acceptance #2): a fix that
merely swallows the flag (or a future edit that widens the filter back to
excluding everything) must not read as "skip always fires" — the second
test pins that a REAL test-file change still surfaces.
"""
from __future__ import annotations

import subprocess


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


def _changed_test_files(base_ref: str, head_ref: str, *, repo_dir: str) -> "list[str]":
    """The EXACT invocation `.github/workflows/test.yml`'s "Run test-tier
    audit" step uses for its own `$CHANGED` — three-dot range, the
    `--diff-filter=d` fix, the `tests/**.py` pathspec."""
    result = subprocess.run(
        [
            "git", "diff", "--name-only", "--diff-filter=d",
            f"{base_ref}...{head_ref}", "--", "tests/**.py",
        ],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_a_deletion_only_branch_reports_no_changed_test_files(tmp_path):
    """Tier 1: accept-side (the #5690 repro) — a branch that ONLY deletes a
    tests/*.py file must produce an EMPTY $CHANGED, so the workflow's
    existing "no test files changed — skip" guard fires instead of handing
    test_tier_audit.py a path that no longer exists."""
    repo_dir = str(tmp_path)
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "test@example.com", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_gone.py").write_text("def test_x(): pass\n")
    (tmp_path / "tests" / "test_keep.py").write_text("def test_y(): pass\n")
    _git("add", "tests/test_gone.py", "tests/test_keep.py", cwd=repo_dir)
    _git("commit", "-q", "-m", "base: add two test files", cwd=repo_dir)

    _git("checkout", "-q", "-b", "feature", cwd=repo_dir)
    _git("rm", "-q", "tests/test_gone.py", cwd=repo_dir)
    _git("commit", "-q", "-m", "feature: remove a dead test file", cwd=repo_dir)
    head_ref = _git("rev-parse", "HEAD", cwd=repo_dir)

    default_branch = "main" if _has_branch(repo_dir, "main") else "master"
    _git("checkout", "-q", default_branch, cwd=repo_dir)
    base_ref = _git("rev-parse", "HEAD", cwd=repo_dir)

    changed = _changed_test_files(base_ref, head_ref, repo_dir=repo_dir)
    assert changed == [], (
        f"a deletion-only branch must report no changed test files, got {changed!r}"
    )


def test_a_genuinely_modified_test_file_still_surfaces(tmp_path):
    """Tier 1: falsify pair for the above — a branch that MODIFIES a real
    test file (even alongside an unrelated deletion) must still list that
    file. Without this, a future edit that widens --diff-filter too far
    (or drops it from `d` to something that excludes M too) would read as
    "always skip" and silently stop auditing real test changes — the exact
    degradation lead-coder's own brief named as the likeliest regression."""
    repo_dir = str(tmp_path)
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "test@example.com", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_edit_me.py").write_text('"""Tier 1: x."""\ndef test_x(): pass\n')
    (tmp_path / "tests" / "test_gone.py").write_text("def test_y(): pass\n")
    _git("add", "tests/test_edit_me.py", "tests/test_gone.py", cwd=repo_dir)
    _git("commit", "-q", "-m", "base: add two test files", cwd=repo_dir)

    _git("checkout", "-q", "-b", "feature", cwd=repo_dir)
    (tmp_path / "tests" / "test_edit_me.py").write_text(
        '"""Tier 1: x, edited."""\ndef test_x(): assert True\n',
    )
    _git("add", "tests/test_edit_me.py", cwd=repo_dir)
    _git("rm", "-q", "tests/test_gone.py", cwd=repo_dir)
    _git("commit", "-q", "-m", "feature: edit one test, delete another", cwd=repo_dir)
    head_ref = _git("rev-parse", "HEAD", cwd=repo_dir)

    default_branch = "main" if _has_branch(repo_dir, "main") else "master"
    _git("checkout", "-q", default_branch, cwd=repo_dir)
    base_ref = _git("rev-parse", "HEAD", cwd=repo_dir)

    changed = _changed_test_files(base_ref, head_ref, repo_dir=repo_dir)
    assert changed == ["tests/test_edit_me.py"], (
        f"a genuinely modified test file must still surface (and the "
        f"deleted one still must not), got {changed!r}"
    )
