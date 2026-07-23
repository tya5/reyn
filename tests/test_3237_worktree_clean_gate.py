"""Tier 1: unlocked-worktree clean-gate reclaim contract (#3237).

Pins the public contract of `classify_unlocked_reclaimability` in
`scripts/cleanup_agent_worktrees.py`: the v2 safety rule that decides
whether an UNLOCKED worktree is safe to reclaim. This is squash-merge safe
(reyn merges via squash-merge + branch-delete, so `git branch -r --contains
HEAD` / `HEAD@{upstream}` are both wrong signals — see the module docstring
in the script for why). The rule keys off `branch.<local>.merge` git config
instead, which survives remote-ref pruning.

Testing policy compliance:
- No MagicMock / AsyncMock / patch. All fixtures are real temp git repos
  exercised through real `git` subprocess calls (a cheaply-real collaborator
  — faking `subprocess.run` would hide signature drift in the git command
  args themselves).
- No private-state assertions — only the public (reclaimable, reason) tuple.
- No algorithm-level pins (exact git porcelain wording, etc.) — only the
  classification outcome.
- Tier declared in this docstring's first line.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _import_cleanup_module():
    """Import scripts/cleanup_agent_worktrees.py as a module.

    Registered in sys.modules before exec so `dataclasses` can resolve the
    module's own `from __future__ import annotations` string annotations
    (WorktreeInfo's fields) via a module lookup.
    """
    module_name = "cleanup_agent_worktrees"
    spec = importlib.util.spec_from_file_location(
        module_name, _SCRIPTS_DIR / "cleanup_agent_worktrees.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cleanup_mod():
    return _import_cleanup_module()


# ---------------------------------------------------------------------------
# Real-git fixture helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}: {result.stderr}"
    )
    return result


def _init_bare_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)], capture_output=True, check=True
    )
    return origin


def _clone_and_seed_branch(
    tmp_path: Path,
    origin: Path,
    name: str,
    branch: str,
) -> Path:
    """Clone `origin`, create `branch`, commit a file, and push -u origin.

    Returns the path to the resulting worktree-like clone.
    """
    wt = tmp_path / name
    subprocess.run(
        ["git", "clone", str(origin), str(wt)], capture_output=True, check=True
    )
    _git(["config", "user.email", "test@example.com"], wt)
    _git(["config", "user.name", "Test"], wt)
    _git(["checkout", "-b", branch], wt)
    (wt / "f.txt").write_text("hello\n")
    _git(["add", "."], wt)
    _git(["commit", "-m", "seed commit"], wt)
    _git(["push", "-u", "origin", branch], wt)
    return wt


def _simulate_squash_merge_and_delete(wt: Path, branch: str) -> None:
    """Simulate reyn's squash-merge + branch-delete: delete the remote branch
    and prune, while the local `branch.<n>.merge`/`.remote` config survives
    (this is the exact scenario the v2 rule must handle — #3231/#3237)."""
    _git(["push", "origin", "--delete", branch], wt)
    _git(["fetch", "--prune"], wt)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_clean_pushed_squash_merged_branch_is_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: clean + pushed + squash-merged-then-deleted branch is reclaimable.

    This is the exact case the naive @{upstream}/--contains rule failed on.
    """
    origin = _init_bare_origin(tmp_path)
    wt = _clone_and_seed_branch(tmp_path, origin, "wt1", "feat-x")
    _simulate_squash_merge_and_delete(wt, "feat-x")

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads={"feat-x"}
    )

    assert reclaimable is True
    assert reason == "reclaimable"


def test_uncommitted_change_is_not_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: an uncommitted modification makes the worktree non-reclaimable."""
    origin = _init_bare_origin(tmp_path)
    wt = _clone_and_seed_branch(tmp_path, origin, "wt2", "feat-dirty")
    _simulate_squash_merge_and_delete(wt, "feat-dirty")
    (wt / "f.txt").write_text("modified\n")

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads={"feat-dirty"}
    )

    assert reclaimable is False
    assert reason == "dirty"


def test_untracked_file_is_not_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: an untracked file makes the worktree non-reclaimable."""
    origin = _init_bare_origin(tmp_path)
    wt = _clone_and_seed_branch(tmp_path, origin, "wt3", "feat-untracked")
    _simulate_squash_merge_and_delete(wt, "feat-untracked")
    (wt / "new_untracked.txt").write_text("surprise\n")

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads={"feat-untracked"}
    )

    assert reclaimable is False
    assert reason == "dirty"


def test_stash_present_is_not_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: a non-empty stash makes the worktree non-reclaimable."""
    origin = _init_bare_origin(tmp_path)
    wt = _clone_and_seed_branch(tmp_path, origin, "wt4", "feat-stash")
    _simulate_squash_merge_and_delete(wt, "feat-stash")
    (wt / "f.txt").write_text("stash me\n")
    _git(["stash"], wt)

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads={"feat-stash"}
    )

    assert reclaimable is False
    assert reason == "stash"


def test_pushed_but_not_in_merged_set_is_not_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: open PR / never merged — branch pushed but absent from merged-set."""
    origin = _init_bare_origin(tmp_path)
    wt = _clone_and_seed_branch(tmp_path, origin, "wt5", "feat-open")
    # No squash-merge simulated; branch still exists on origin (open PR case).

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads={"some-other-merged-branch"}
    )

    assert reclaimable is False
    assert reason == "no-merged-pr"


def test_never_pushed_upstream_is_not_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: no `branch.<n>.merge` config — push never set an upstream."""
    origin = _init_bare_origin(tmp_path)
    wt = tmp_path / "wt6"
    subprocess.run(
        ["git", "clone", str(origin), str(wt)], capture_output=True, check=True
    )
    _git(["config", "user.email", "test@example.com"], wt)
    _git(["config", "user.name", "Test"], wt)
    _git(["checkout", "-b", "feat-local-only"], wt)
    (wt / "f.txt").write_text("hello\n")
    _git(["add", "."], wt)
    _git(["commit", "-m", "local only"], wt)
    # deliberately never pushed

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads={"feat-local-only"}
    )

    assert reclaimable is False
    assert reason == "no-upstream-config"


def test_remote_other_than_origin_is_not_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: pushed branch's remote is not `origin` — must keep."""
    origin = _init_bare_origin(tmp_path)
    other_remote_dir = tmp_path / "other_origin_parent"
    other_remote_dir.mkdir()
    other_remote = _init_bare_origin(other_remote_dir)
    wt = tmp_path / "wt7"
    subprocess.run(
        ["git", "clone", str(origin), str(wt)], capture_output=True, check=True
    )
    _git(["config", "user.email", "test@example.com"], wt)
    _git(["config", "user.name", "Test"], wt)
    _git(["checkout", "-b", "feat-other-remote"], wt)
    (wt / "f.txt").write_text("hello\n")
    _git(["add", "."], wt)
    _git(["commit", "-m", "seed"], wt)
    _git(["remote", "add", "other", str(other_remote)], wt)
    _git(["push", "-u", "other", "feat-other-remote"], wt)

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads={"feat-other-remote"}
    )

    assert reclaimable is False
    assert reason == "wrong-remote"


def test_detached_head_is_not_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: detached HEAD (no symbolic-ref) — defensive KEEP."""
    origin = _init_bare_origin(tmp_path)
    wt = _clone_and_seed_branch(tmp_path, origin, "wt8", "feat-detach")
    _simulate_squash_merge_and_delete(wt, "feat-detach")
    _git(["checkout", "--detach", "HEAD"], wt)

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads={"feat-detach"}
    )

    assert reclaimable is False
    assert reason == "detached-head"


def test_git_error_on_nonexistent_path_is_not_reclaimable(cleanup_mod, tmp_path):
    """Tier 1: a path that isn't a git repo at all must fail safe (defensive)."""
    not_a_repo = tmp_path / "does_not_exist"

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        not_a_repo, merged_heads={"anything"}
    )

    assert reclaimable is False
    assert reason == "git-error"


def test_merged_set_unavailable_fails_safe_to_keep(cleanup_mod, tmp_path):
    """Tier 1: gh unavailable (merged_heads=None) must KEEP even a
    clean+pushed worktree whose branch would otherwise qualify."""
    origin = _init_bare_origin(tmp_path)
    wt = _clone_and_seed_branch(tmp_path, origin, "wt9", "feat-gh-down")
    _simulate_squash_merge_and_delete(wt, "feat-gh-down")

    reclaimable, reason = cleanup_mod.classify_unlocked_reclaimability(
        wt, merged_heads=None
    )

    assert reclaimable is False
    assert reason == "gh-unavailable"
