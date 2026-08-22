"""Tier 1/2: `session_position.py` — the "own position" facts for
`describe_session` (#5012-A). Real temp git repos, no mocks.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from reyn.runtime.session_position import (
    capability_probe,
    describe_session_position,
    git_position,
    venv_position,
)

_COMMIT_SUMMARY_SHA_RE = re.compile(r"\[\S+ (?:\(root-commit\) )?([0-9a-f]+)\]")


def _commit_and_get_short_sha(repo: Path, *, message: str = "init") -> str:
    """Commit and return the short SHA `git commit`'s own summary line
    reports — a stable, documented CLI output contract, not an internal
    storage-format read (lead-coder catch, #5012-A review round 2:
    `.git/refs/heads/<branch>` stops existing once refs are packed via
    `git gc`/`git pack-refs`, so reading that file directly ties a test to
    an implementation detail that can silently stop holding)."""
    result = subprocess.run(
        ["git", "commit", "-m", message], cwd=repo, capture_output=True, text=True, check=True,
    )
    match = _COMMIT_SUMMARY_SHA_RE.search(result.stdout)
    assert match, f"could not parse a short SHA out of: {result.stdout!r}"
    return match.group(1)


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def test_git_position_reports_branch_and_head_for_a_real_commit(tmp_path: Path) -> None:
    """Tier 1: a real repo with one commit on a named branch reports both.

    The expected SHA comes from `git commit`'s OWN summary-line output
    (`_commit_and_get_short_sha`) — a stable, documented CLI contract —
    rather than a second `git rev-parse HEAD` call (blind to "both sides
    wrong the same way", CLAUDE.md test-review §2) or a direct read of
    `.git/refs/heads/main` (lead-coder catch, round 2: that file stops
    existing once refs are packed via `git gc`/`git pack-refs` — an
    internal storage-format assumption, not a stable contract)."""
    repo = _init_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    short_sha = _commit_and_get_short_sha(repo)

    position = git_position(repo)

    assert position["branch"] == "main"
    assert position["head"] is not None
    assert position["head"].startswith(short_sha)


def test_git_position_reports_none_for_a_repo_with_no_commits(tmp_path: Path) -> None:
    """Tier 1: a freshly-initialized repo has no HEAD yet — None, not a
    fabricated SHA. FALSIFY: without this, a caller could mistake a real
    absence for a real value."""
    repo = _init_repo(tmp_path)

    position = git_position(repo)

    assert position["head"] is None


def test_git_position_reports_none_branch_on_detached_head(tmp_path: Path) -> None:
    """Tier 1: a detached HEAD has no branch NAME — `--abbrev-ref` returns
    the literal string "HEAD", which this module normalizes to None rather
    than reporting "HEAD" as if it were a real branch."""
    repo = _init_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    short_sha = _commit_and_get_short_sha(repo)
    # `git checkout` accepts an unambiguous short SHA directly — same
    # independent-source reasoning as the real-commit test above.
    subprocess.run(["git", "checkout", "-q", short_sha], cwd=repo, check=True)

    position = git_position(repo)

    assert position["branch"] is None
    assert position["head"] is not None
    assert position["head"].startswith(short_sha)


def test_git_position_reports_none_for_a_non_git_directory(tmp_path: Path) -> None:
    """Tier 1: a directory that isn't a git repo at all — both fields None,
    no exception raised."""
    position = git_position(tmp_path)

    assert position == {"branch": None, "head": None}


def test_venv_position_reports_the_real_running_interpreter() -> None:
    """Tier 1: reads the actual interpreter this test is running under —
    not a hardcoded example path."""
    position = venv_position()

    assert position["python_executable"] == sys.executable
    assert position["venv_path"] == sys.prefix


def test_capability_probe_key_set_is_the_declared_three() -> None:
    """Tier 1: capability_probe() reports exactly {ruff, pytest, mkdocs} —
    reyn's own contract (the CLAUDE.md toolchain), independent of whether
    any of the three happens to be installed in the environment this test
    runs in."""
    assert set(capability_probe().keys()) == {"ruff", "pytest", "mkdocs"}


def test_capability_probe_reflects_the_injected_resolver_both_directions() -> None:
    """Tier 1: capability_probe()'s CONTRACT is "return a boolean per named
    tool derived from the injected resolver's present/absent result" — not
    "shutil.which is correct" (that is the standard library's contract, not
    reyn's; #5012-A PR #5038 review, issuecomment-5376723503: the original
    version of this test re-derived `shutil.which(tool) is not None` on the
    assert side, the same expression as the implementation, which cannot
    fail for a reyn-side bug and cannot construct an absent case either).

    Constructs BOTH a present and an absent case via the injectable
    ``resolve`` seam, so the boolean-collapse itself is under test, not
    retraced."""
    always_present = capability_probe(resolve=lambda _tool: "/usr/bin/tool")
    always_absent = capability_probe(resolve=lambda _tool: None)

    assert always_present == {"ruff": True, "pytest": True, "mkdocs": True}
    assert always_absent == {"ruff": False, "pytest": False, "mkdocs": False}


def test_describe_session_position_assembles_all_fields(tmp_path: Path) -> None:
    """Tier 2: the assembled bundle carries every named field, sourced from
    the real repo passed in — not a stale example from a different repo."""
    repo = _init_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    result = describe_session_position(repo)

    assert result["repo_root"] == str(repo)
    assert result["branch"] == "main"
    assert result["head"] is not None
    assert result["python_executable"] == sys.executable
    assert result["venv_path"] == sys.prefix
    assert set(result["capability"].keys()) == {"ruff", "pytest", "mkdocs"}
