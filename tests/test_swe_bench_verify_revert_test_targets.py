"""Tier 2: FP-0008 C6 — verify preprocessor reverts test_patch targets before apply.

Root cause: the apply phase may edit test files (no structural prohibition).
The verify phase's ``git apply test_patch`` then fails with
``error: patch failed: <path>: does not match index`` because the apply LLM
modified a file the test_patch also targets.  Verify transitions back to
apply → retry loop → ``loop_limit_exceeded``.

Fix shape (C6, deterministic per PR-N15/C2 pattern):
  - verify.md preprocessor gains two new steps (after sanitize_test_patch):
    1. ``run_op: shell: cmd: pwd`` → ``data._repo_dir`` (workspace cwd)
    2. ``python: revert_test_targets`` → ``data._revert_result``
       Parses ``+++ b/<path>`` targets from ``data.test_patch`` and runs
       ``git checkout HEAD -- <path>`` for each, using the repo_dir captured
       by step 1 (correct cwd, safe for concurrent benchmarks).
  - report.md gains the same revert preprocessor so ``git diff HEAD``
    produces a source-only patch.
  - apply.md / plan.md gain a source-only domain rule (Part 3 nudge).

This file pins:
  A. The revert mechanism (real git repo fixture, real subprocess, no mocks):
     working-tree test-file contamination → revert → ``git apply`` succeeds.
  B. The final-diff mechanism: source edit present, test-target edit present
     → after revert, ``git diff HEAD`` contains source path, not test path.
  C. No-op safety: empty test_patch / clean tree → graceful no-op (no error).
  D. Target parsing: ``_parse_test_patch_targets`` extracts correct paths.
  E. verify.md preprocessor has the pwd + revert steps in the right order.
  F. report.md preprocessor has the revert step.
  G. apply.md + plan.md have the source-only domain rule.

Tier rule discipline:
  - Every test docstring opens with ``Tier 2:``.
  - Real git repo via ``tmp_path`` (git init + commit + modify).
  - Real subprocess git + real files. No MagicMock / AsyncMock / patch.
  - No private-state assertions.
  - No format pinning (no ``len(x) == N`` assertions on algorithm internals).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SKILL_ROOT = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)


# ── Git repo fixture ──────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in ``cwd``; raise on non-zero unless ``check=False``."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _setup_repo(tmp_path: Path) -> Path:
    """Create a git repo with one source file and one test file committed.

    Returns the repo root path.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@test.com"], cwd=repo)
    _git(["config", "user.name", "Test User"], cwd=repo)
    # Source file
    (repo / "mymod.py").write_text("def foo():\n    return 1\n")
    # Test file
    (repo / "test_mymod.py").write_text("def test_foo():\n    assert foo() == 1\n")
    _git(["add", "mymod.py", "test_mymod.py"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


def _make_test_patch(repo: Path) -> str:
    """Build a minimal unified diff that touches ``test_mymod.py``."""
    return (
        "diff --git a/test_mymod.py b/test_mymod.py\n"
        "--- a/test_mymod.py\n"
        "+++ b/test_mymod.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def test_foo():\n"
        "     assert foo() == 1\n"
        "+    assert foo() != 2\n"
    )


# ── Test A: revert mechanism unblocks git apply ───────────────────────────────


def test_revert_unblocks_git_apply(tmp_path: pytest.TempdirFactory) -> None:
    """Tier 2: Part 1 — contaminated test file reverted; git apply then succeeds.

    Simulates the C6 defect:
    1. Repo committed with test_mymod.py at HEAD.
    2. Apply phase contaminates the file (writes different content).
    3. test_patch targets test_mymod.py — without revert, ``git apply`` fails.
    4. After revert_test_targets, the file is back at HEAD content.
    5. ``git apply <test_patch>`` now succeeds (returncode 0).

    This directly proves the loop-unblock: verify can proceed instead of
    transitioning back to apply.
    """
    from reyn.stdlib.skills.swe_bench.revert_test_targets import revert_test_targets

    repo = _setup_repo(tmp_path)
    test_patch = _make_test_patch(repo)

    # Simulate apply-phase contamination: write different content to the test file
    (repo / "test_mymod.py").write_text(
        "# apply-phase contamination\n"
        "def test_foo():\n"
        "    assert foo() == 99  # wrong, apply LLM wrote this\n"
    )

    # Verify that git apply FAILS before the revert (proves the defect)
    patch_file = repo / ".reyn_test.patch"
    patch_file.write_text(test_patch)
    pre_result = subprocess.run(
        ["git", "apply", "--check", str(patch_file)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert pre_result.returncode != 0, (
        "Pre-condition: git apply must fail on contaminated test file. "
        f"Unexpectedly succeeded; test_patch may not target test_mymod.py. "
        f"stderr: {pre_result.stderr}"
    )

    # Run the revert function (the C6 fix)
    result = revert_test_targets({
        "test_patch": test_patch,
        "_repo_dir": str(repo),
    })

    # Assert test file is back at HEAD content
    head_content = (repo / "test_mymod.py").read_text()
    assert "apply-phase contamination" not in head_content, (
        "test_mymod.py must be reverted to HEAD — contamination still present. "
        f"Content: {head_content!r}"
    )
    assert "def test_foo" in head_content, (
        "test_mymod.py must contain the original HEAD content after revert. "
        f"Content: {head_content!r}"
    )

    # Assert revert function reported success
    assert "test_mymod.py" in result["reverted"], (
        f"revert_test_targets must report test_mymod.py as reverted. "
        f"result['reverted']: {result['reverted']}"
    )

    # Assert git apply NOW SUCCEEDS — this is the loop-unblock proof
    post_result = subprocess.run(
        ["git", "apply", str(patch_file)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert post_result.returncode == 0, (
        "After revert, git apply <test_patch> must succeed (returncode 0). "
        f"This proves the apply×verify loop is unblocked. "
        f"returncode={post_result.returncode}, stderr={post_result.stderr!r}"
    )


# ── Test B: final diff excludes test-target path ─────────────────────────────


def test_final_diff_excludes_test_target(tmp_path: pytest.TempdirFactory) -> None:
    """Tier 2: Part 2 — final diff is source-only after revert.

    Working tree has BOTH a source edit (mymod.py) and a test-target edit
    (test_mymod.py, simulating apply-phase contamination).
    After revert_test_targets, the diff must contain the source path and
    must NOT contain the test-target path.
    """
    from reyn.stdlib.skills.swe_bench.revert_test_targets import revert_test_targets

    repo = _setup_repo(tmp_path)
    test_patch = _make_test_patch(repo)

    # Edit source file (legitimate fix)
    (repo / "mymod.py").write_text("def foo():\n    return 2  # fixed\n")
    # Edit test file (apply-phase contamination, should be excluded)
    (repo / "test_mymod.py").write_text(
        "def test_foo():\n    assert foo() == 2  # contamination\n"
    )

    # Verify both files are dirty before revert
    diff_before = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "mymod.py" in diff_before, "Pre-condition: source file must be modified"
    assert "test_mymod.py" in diff_before, "Pre-condition: test file must be modified"

    # Run revert (reverting only the test_patch-targeted file)
    result = revert_test_targets({
        "test_patch": test_patch,
        "_repo_dir": str(repo),
    })
    assert "test_mymod.py" in result["reverted"], (
        f"test_mymod.py must be reported as reverted. result={result}"
    )

    # Capture the final diff after revert
    diff_after = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # Source path must still be in the diff
    assert "mymod.py" in diff_after, (
        "Final diff must contain the source edit (mymod.py). "
        f"diff_after: {diff_after!r}"
    )
    # Test-target path must NOT be in the diff
    assert "test_mymod.py" not in diff_after, (
        "Final diff must NOT contain the test-target path (test_mymod.py) "
        "after revert. The solution patch must be source-only. "
        f"diff_after: {diff_after!r}"
    )


# ── Test C: no-op safety ──────────────────────────────────────────────────────


def test_no_op_on_empty_test_patch() -> None:
    """Tier 2: Part 1 — empty/absent test_patch → graceful no-op, no error."""
    from reyn.stdlib.skills.swe_bench.revert_test_targets import revert_test_targets

    result = revert_test_targets({"test_patch": "", "_repo_dir": "/nonexistent"})
    assert result == {"reverted": [], "errors": []}, (
        f"Empty test_patch must produce empty result without error. "
        f"Got: {result}"
    )


def test_no_op_on_absent_test_patch() -> None:
    """Tier 2: no test_patch field → graceful no-op, no error."""
    from reyn.stdlib.skills.swe_bench.revert_test_targets import revert_test_targets

    result = revert_test_targets({"instance_id": "test-1"})
    assert result == {"reverted": [], "errors": []}, (
        f"Absent test_patch must produce empty result without error. "
        f"Got: {result}"
    )


def test_no_op_on_missing_repo_dir() -> None:
    """Tier 2: no _repo_dir → graceful no-op (cannot revert without cwd)."""
    from reyn.stdlib.skills.swe_bench.revert_test_targets import revert_test_targets

    test_patch = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    result = revert_test_targets({"test_patch": test_patch})
    assert result == {"reverted": [], "errors": []}, (
        f"Missing _repo_dir must produce empty result without error. "
        f"Got: {result}"
    )


def test_no_op_on_clean_tree(tmp_path: pytest.TempdirFactory) -> None:
    """Tier 2: clean working tree → revert runs without error (checkout is a no-op)."""
    from reyn.stdlib.skills.swe_bench.revert_test_targets import revert_test_targets

    repo = _setup_repo(tmp_path)
    test_patch = _make_test_patch(repo)

    # Working tree is clean (no contamination)
    result = revert_test_targets({
        "test_patch": test_patch,
        "_repo_dir": str(repo),
    })
    # Should succeed (git checkout HEAD -- on an already-clean file is a no-op)
    assert isinstance(result, dict), "Result must be a dict"
    assert "reverted" in result and "errors" in result, (
        f"Result must have 'reverted' and 'errors' keys. Got: {result}"
    )
    # No errors expected when tree is clean
    assert not result["errors"], (
        f"No errors expected on a clean tree. Got errors: {result['errors']}"
    )


# ── Test D: target parsing ────────────────────────────────────────────────────


def test_parse_targets_extracts_plus_plus_plus_paths() -> None:
    """Tier 2: _parse_test_patch_targets extracts +++ b/<path> headers correctly."""
    from reyn.stdlib.skills.swe_bench.revert_test_targets import _parse_test_patch_targets

    patch = (
        "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
        "--- a/tests/test_foo.py\n"
        "+++ b/tests/test_foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
        "diff --git a/tests/test_bar.py b/tests/test_bar.py\n"
        "--- a/tests/test_bar.py\n"
        "+++ b/tests/test_bar.py\n"
        "@@ -1 +1 @@\n"
        "-a\n+b\n"
    )
    targets = _parse_test_patch_targets(patch)
    assert "tests/test_foo.py" in targets, (
        f"Must extract tests/test_foo.py. Got: {targets}"
    )
    assert "tests/test_bar.py" in targets, (
        f"Must extract tests/test_bar.py. Got: {targets}"
    )


def test_parse_targets_excludes_dev_null() -> None:
    """Tier 2: _parse_test_patch_targets skips /dev/null targets (new files)."""
    from reyn.stdlib.skills.swe_bench.revert_test_targets import _parse_test_patch_targets

    patch = (
        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
        "--- /dev/null\n"
        "+++ b/tests/test_new.py\n"
        "@@ -0,0 +1 @@\n"
        "+new file\n"
    )
    targets = _parse_test_patch_targets(patch)
    # /dev/null is the "from" side; the "+++ b/tests/test_new.py" is still a target
    # (it's a new file, but it exists after the patch — reverts are safe)
    # The "/dev/null" itself should not appear in targets
    assert "/dev/null" not in targets, (
        f"/dev/null must not appear in parsed targets. Got: {targets}"
    )


def test_parse_targets_deduplicates() -> None:
    """Tier 2: _parse_test_patch_targets deduplicates repeated paths."""
    from reyn.stdlib.skills.swe_bench.revert_test_targets import _parse_test_patch_targets

    # Same file appears twice (unusual but possible in some edge-case patches)
    patch = (
        "+++ b/tests/test_foo.py\n"
        "+++ b/tests/test_foo.py\n"
    )
    targets = _parse_test_patch_targets(patch)
    assert targets.count("tests/test_foo.py") == 1, (
        f"Deduplicated path must appear exactly once. Got: {targets}"
    )


def test_parse_targets_empty_patch_returns_empty() -> None:
    """Tier 2: _parse_test_patch_targets returns empty list for empty patch."""
    from reyn.stdlib.skills.swe_bench.revert_test_targets import _parse_test_patch_targets

    targets = _parse_test_patch_targets("")
    assert targets == [], f"Empty patch must produce empty target list. Got: {targets}"


# ── Test E: verify.md preprocessor has pwd + revert steps ────────────────────


def test_verify_md_has_pwd_step_before_revert_step() -> None:
    """Tier 2: verify.md preprocessor has a pwd shell step before the revert python step.

    The revert_test_targets function requires data._repo_dir (from `pwd`) to
    know the correct cwd. The pwd step must precede the revert step.
    """
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")

    # Both steps must be present
    assert "cmd: pwd" in verify_md, (
        "verify.md must contain a 'cmd: pwd' preprocessor step to capture repo_dir"
    )
    assert "revert_test_targets" in verify_md, (
        "verify.md must reference the revert_test_targets function"
    )

    # pwd must appear before revert
    pwd_pos = verify_md.find("cmd: pwd")
    revert_pos = verify_md.find("revert_test_targets")
    assert pwd_pos < revert_pos, (
        "The 'cmd: pwd' step must appear BEFORE 'revert_test_targets' in "
        "verify.md preprocessor so that data._repo_dir is available when revert runs. "
        f"pwd_pos={pwd_pos}, revert_pos={revert_pos}"
    )


def test_verify_md_revert_step_has_on_error_empty() -> None:
    """Tier 2: verify.md revert preprocessor step is graceful (on_error: empty discipline).

    The revert step must be fault-tolerant — in unit tests or when the repo_dir
    is unavailable, it must not crash the preprocessor.
    """
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    # The _repo_dir shell step (pwd) must have on_error: empty
    assert "on_error: empty" in verify_md, (
        "verify.md must have at least one 'on_error: empty' for the pwd / revert steps"
    )


def test_verify_md_revert_into_path() -> None:
    """Tier 2: verify.md revert step stores result at data._revert_result."""
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    assert "data._revert_result" in verify_md, (
        "verify.md revert step must use 'into: data._revert_result' "
        "so the result is stored in the artifact for debugging"
    )


def test_verify_md_repo_dir_into_path() -> None:
    """Tier 2: verify.md pwd step stores result at data._repo_dir."""
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    assert "data._repo_dir" in verify_md, (
        "verify.md must store pwd result at 'into: data._repo_dir' "
        "so revert_test_targets can read it"
    )


# ── Test F: report.md preprocessor has the revert step ───────────────────────


def test_report_md_has_revert_step() -> None:
    """Tier 2: report.md preprocessor has the revert_test_targets step.

    Part 2: the report phase must also revert test_patch targets before
    issuing ``git diff HEAD``, ensuring the final solution patch is
    source-only even if verify's Step 3 left residual state.
    """
    report_md = (_SKILL_ROOT / "phases" / "report.md").read_text(encoding="utf-8")
    assert "revert_test_targets" in report_md, (
        "report.md must reference the revert_test_targets function in its preprocessor"
    )
    assert "preprocessor:" in report_md, (
        "report.md must have a preprocessor block"
    )


def test_report_md_reads_workspace_input() -> None:
    """Tier 2: report.md preprocessor reads workspace _input to get test_patch.

    The verify_state artifact doesn't carry test_patch; the report preprocessor
    must read it from the workspace _input artifact (same source as verify).
    """
    report_md = (_SKILL_ROOT / "phases" / "report.md").read_text(encoding="utf-8")
    assert "swe_bench/_input/v01_swe_bench_input.json" in report_md, (
        "report.md must reference the workspace _input artifact path to access test_patch"
    )


# ── Test G: apply.md + plan.md have source-only rule ─────────────────────────


def test_apply_md_has_source_only_rule() -> None:
    """Tier 2: apply.md has the source-only domain rule (Part 3 nudge).

    The rule must indicate that the SWE-bench harness owns test files and that
    edits to test files are reverted. This is a domain rule (P8-legal), not a
    Control-IR instruction.
    """
    apply_md = (_SKILL_ROOT / "phases" / "apply.md").read_text(encoding="utf-8")
    assert "SOURCE files only" in apply_md or "source files only" in apply_md.lower(), (
        "apply.md must contain the 'SOURCE files only' domain rule"
    )
    # Must mention that test edits are reverted / won't count
    assert "reverted" in apply_md or "harness owns" in apply_md, (
        "apply.md source-only rule must mention that test edits are reverted "
        "or that the harness owns test files"
    )


def test_plan_md_has_source_only_rule() -> None:
    """Tier 2: plan.md has the source-only domain rule (Part 3 nudge).

    The planner must be instructed not to include test files in the edit plan.
    """
    plan_md = (_SKILL_ROOT / "phases" / "plan.md").read_text(encoding="utf-8")
    assert "SOURCE files only" in plan_md or "source files only" in plan_md.lower(), (
        "plan.md must contain the 'SOURCE files only' domain rule"
    )


# ── Test: revert with full artifact shape (verify phase shape) ────────────────


def test_revert_reads_from_inner_data_test_patch(
    tmp_path: pytest.TempdirFactory,
) -> None:
    """Tier 2: revert_test_targets reads test_patch from inner data dict.

    In the verify phase, after sanitize_test_patch runs, data.test_patch
    is set in the inner data dict (full artifact shape). This test ensures
    the function uses it correctly.
    """
    from reyn.stdlib.skills.swe_bench.revert_test_targets import revert_test_targets

    repo = _setup_repo(tmp_path)
    test_patch = _make_test_patch(repo)

    # Contaminate the test file
    (repo / "test_mymod.py").write_text("# contaminated\ndef test_foo(): pass\n")

    # Full artifact shape (as seen in verify phase after sanitize_test_patch)
    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test-1",
            "files_edited": ["mymod.py"],
            "attempt": 1,
            "test_patch": test_patch,   # set by sanitize_test_patch into: data.test_patch
            "_repo_dir": {              # set by run_op: shell: cmd: pwd
                "status": "ok",
                "stdout": str(repo),
                "stderr": "",
                "returncode": 0,
            },
        },
    }
    result = revert_test_targets(artifact)
    assert "test_mymod.py" in result["reverted"], (
        f"Must revert test_mymod.py from full artifact shape. result={result}"
    )
    head_content = (repo / "test_mymod.py").read_text()
    assert "contaminated" not in head_content, (
        "test_mymod.py must be at HEAD content after revert via full artifact shape"
    )
