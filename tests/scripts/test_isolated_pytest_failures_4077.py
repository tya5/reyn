"""Tier 2: #4077 — the isolated-pytest-failures scanner.

Placed in tests/scripts/ (mirrors the other scripts/ test files' own
placement rationale). Real subprocesses throughout for run_one_file (a
thin wrapper over `subprocess.run`/pytest itself, so faking either would
test nothing real) — the fixture test files are tiny (one trivial
pass/fail/hang each), so real runs stay fast.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.isolated_pytest_failures import (
    ci_pytest_flags,
    collected_test_files,
    run_one_file,
)


def test_ci_pytest_flags_parses_the_real_workflow_line() -> None:
    """Tier 2: reads .github/workflows/test.yml's actual pytest invocation
    — not hand-copied flags that could silently drift from what CI runs."""
    flags = ci_pytest_flags()
    assert "-n" in flags and "auto" in flags
    assert any(f.startswith("--timeout=") for f in flags)


def test_ci_pytest_flags_raises_loudly_when_the_line_is_missing(tmp_path: Path) -> None:
    """Tier 2: falsification — a workflow file with no matching pytest
    invocation line must fail LOUDLY (a clear ValueError), not silently
    return an empty/wrong flag list that would make every subsequent scan
    run pytest with no flags at all, unnoticed."""
    workflow = tmp_path / "test.yml"
    workflow.write_text("name: CI\njobs:\n  build:\n    steps: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="could not find"):
        ci_pytest_flags(workflow)


def test_collected_test_files_excludes_scaffold(tmp_path: Path) -> None:
    """Tier 2: tests/scaffold/ is excluded — a migration-lifespan bucket
    with its own triggered_by/removed_by churn, not a stable population to
    scan (see module docstring)."""
    tests_dir = tmp_path / "tests"
    (tests_dir / "core").mkdir(parents=True)
    (tests_dir / "core" / "test_a.py").write_text("", encoding="utf-8")
    (tests_dir / "scaffold").mkdir(parents=True)
    (tests_dir / "scaffold" / "test_b.py").write_text("", encoding="utf-8")

    files = collected_test_files(tests_dir)

    names = {p.name for p in files}
    assert names == {"test_a.py"}


def test_collected_test_files_only_matches_test_star_py(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — a file that doesn't match pytest's own
    collection glob (test_*.py) is not swept in just because it lives
    under tests/."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_real.py").write_text("", encoding="utf-8")
    (tests_dir / "conftest.py").write_text("", encoding="utf-8")
    (tests_dir / "helpers.py").write_text("", encoding="utf-8")

    files = collected_test_files(tests_dir)

    assert [p.name for p in files] == ["test_real.py"]


def test_run_one_file_classifies_a_passing_file(tmp_path: Path) -> None:
    """Tier 2: a real, trivially-passing test file classifies as
    'passed', with no excerpt key (nothing to triage)."""
    root = tmp_path
    (root / "test_ok.py").write_text(
        "def test_it():\n    assert True\n", encoding="utf-8",
    )
    result = run_one_file(root / "test_ok.py", ["-q"], root=root, per_file_timeout=30)
    assert result["outcome"] == "passed"
    assert "excerpt" not in result


def test_run_one_file_classifies_a_failing_file(tmp_path: Path) -> None:
    """Tier 2: falsification pair — the SAME machinery on a real failing
    assertion classifies as 'failed', with an excerpt for triage."""
    root = tmp_path
    (root / "test_bad.py").write_text(
        "def test_it():\n    assert False, 'deliberate failure'\n", encoding="utf-8",
    )
    result = run_one_file(root / "test_bad.py", ["-q"], root=root, per_file_timeout=30)
    assert result["outcome"] == "failed"
    assert "deliberate failure" in result["excerpt"]


def test_run_one_file_classifies_a_hanging_file_as_hung(tmp_path: Path) -> None:
    """Tier 2: a real file that never returns within the per-file timeout
    classifies as 'hung', not 'failed' — the two are reported separately
    (#4077's own motivating distinction: CI's own timeout wrapper turns a
    hang into a failure at the JOB level, but a per-FILE hang and a
    per-FILE assertion failure are different triage stories)."""
    root = tmp_path
    (root / "test_hangs.py").write_text(
        "import time\n\ndef test_it():\n    time.sleep(30)\n", encoding="utf-8",
    )
    result = run_one_file(root / "test_hangs.py", ["-q"], root=root, per_file_timeout=2)
    assert result["outcome"] == "hung"
