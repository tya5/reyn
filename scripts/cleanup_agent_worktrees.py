#!/usr/bin/env python3
"""Cleanup stale agent worktrees from .claude/worktrees/agent-* paths.

Detects worktrees whose lock file references a dead PID and removes them
safely, skipping any whose process is still alive. Also reclaims UNLOCKED
worktrees whose branch was pushed and merged (squash-merge + branch-delete
safe — see `classify_unlocked_reclaimability` for the safety criterion,
#3237).

Usage:
    python scripts/cleanup_agent_worktrees.py --list        # default: show breakdown
    python scripts/cleanup_agent_worktrees.py --dry-run     # simulate cleanup
    python scripts/cleanup_agent_worktrees.py --force       # remove stale + merged+clean unlocked
    python scripts/cleanup_agent_worktrees.py --keep-recent 5 --force
    python scripts/cleanup_agent_worktrees.py --include-alive --dry-run  # dangerous
    python scripts/cleanup_agent_worktrees.py --include-dirty --dry-run  # dangerous
    python scripts/cleanup_agent_worktrees.py --json        # machine-readable
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_PREFIX = ".claude/worktrees/agent-"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    locked: bool
    pid: int | None = None
    alive: bool | None = None  # None = unknown (no pid)
    reclaimable: bool | None = None  # unlocked only; None = not evaluated (locked)
    reclaim_reason: str | None = None  # unlocked only; see classify_unlocked_reclaimability
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self.path.name

    @property
    def stale(self) -> bool:
        """Dead-pid locked worktree — cleanup candidate."""
        return self.locked and self.pid is not None and self.alive is False

    @property
    def status_label(self) -> str:
        if not self.locked:
            return "unlocked"
        if self.pid is None:
            return "locked/no-pid"
        if self.alive:
            return "alive"
        return "dead"


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def get_agent_worktrees() -> list[WorktreeInfo]:
    """Parse `git worktree list` and return agent-prefix entries only."""
    result = subprocess.run(
        ["git", "worktree", "list"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        print(f"ERROR: git worktree list failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    worktrees: list[WorktreeInfo] = []
    for line in result.stdout.splitlines():
        if AGENT_PREFIX not in line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = Path(parts[0])
        branch_raw = parts[2] if len(parts) > 2 else ""
        branch = branch_raw.strip("[]")
        locked = "locked" in line
        worktrees.append(WorktreeInfo(path=path, branch=branch, locked=locked))

    return worktrees


def get_lock_pid(worktree: WorktreeInfo) -> int | None:
    """Read .git/worktrees/<name>/locked and extract PID if present."""
    locked_file = REPO_ROOT / ".git" / "worktrees" / worktree.name / "locked"
    if not locked_file.exists():
        return None
    content = locked_file.read_text(encoding="utf-8").strip()
    m = re.search(r"pid\s+(\d+)", content)
    if m:
        return int(m.group(1))
    return None


def is_pid_alive(pid: int) -> bool:
    """Check whether a process is alive via `ps -p <pid>` (macOS + Linux)."""
    try:
        subprocess.run(
            ["ps", "-p", str(pid)],
            capture_output=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def enrich(worktrees: list[WorktreeInfo]) -> None:
    """Fill in pid + alive fields in-place."""
    for wt in worktrees:
        if wt.locked:
            wt.pid = get_lock_pid(wt)
            if wt.pid is not None:
                wt.alive = is_pid_alive(wt.pid)


# ---------------------------------------------------------------------------
# Unlocked-worktree clean-gate reclaim (#3237)
# ---------------------------------------------------------------------------
#
# reyn merges via squash-merge + branch-delete. Squash creates a NEW commit,
# so a worktree's original commits are never ancestors of origin/main:
# `git branch -r --contains HEAD` is empty and `HEAD@{upstream}` ERRORS once
# the remote ref is pruned (`fatal: ambiguous argument '...@{upstream}':
# unknown revision`) — both signals are wrong for exactly the case we target
# and must not be reintroduced.
#
# The safe v3 rule keys off `branch.<local>.merge` git config instead: it is
# a pure local-config read that SURVIVES remote-ref pruning (verified), so it
# still resolves the worktree's pushed branch name after squash-merge +
# branch-delete. Cross-referenced against the merged-PR head set fetched
# once via `gh pr list --state merged`.


def fetch_merged_head_set() -> set[str] | None:
    """
    Fetch head branch names of all merged PRs via `gh`, once (not per-worktree).

    Returns the set of head ref names, or None if `gh` is unavailable for
    ANY reason (offline, no auth, error, timeout). Callers MUST fail safe on
    None: treat the merged-PR signal as unavailable and refuse to reclaim
    any unlocked worktree rather than guessing.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--limit",
                "5000",  # default is 30 — an under-fetch would wrongly KEEP old merged worktrees
                "--json",
                "headRefName",
                "--jq",
                ".[].headRefName",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def classify_unlocked_reclaimability(
    worktree_path: Path, merged_heads: set[str] | None
) -> tuple[bool, str]:
    """
    Decide whether an UNLOCKED worktree is safe to reclaim (v3 rule, #3237).

    Reclaimable iff ALL of:
      1. `git status --porcelain` is empty (no uncommitted/untracked changes)
      2. it was pushed to `origin` and its pushed branch has a merged PR —
         keyed via `branch.<local>.merge` config (NOT `@{upstream}`, which
         errors after the remote ref is pruned post squash-merge-and-delete)

    NOTE: `git stash` is deliberately NOT part of this gate. The stash ref
    lives in the shared `.git` dir, not per-worktree — every worktree of the
    same repo (including `main`) sees the SAME stash list. A stash is
    therefore neither evidence of *this* worktree's state nor a loss vector
    on removal: `git worktree remove` never touches the shared stash, so
    reclaiming a worktree can never destroy stashed work (#3237 v3 — an
    earlier revision wrongly gated on stash and, in practice, saw every
    worktree in a real repo classified as "stash" because they all shared
    one common-repo entry).

    Returns (reclaimable, reason). `reason` is one of: "reclaimable",
    "dirty", "detached-head", "no-upstream-config", "wrong-remote",
    "no-merged-pr", "gh-unavailable", "git-error".

    Fail-safe by construction: any git command erroring, a detached HEAD, a
    missing `branch.<local>.merge`/`.remote` config (push never set
    upstream), a remote other than `origin`, or an unavailable merged-PR set
    (`merged_heads is None`) all resolve to `False` (KEEP) — never reclaim
    on uncertainty.

    Known residual (documented, not solved here): unpushed commits made
    AFTER a merge are not captured by this signal — such a worktree would
    read as clean + merged yet hold unpushed work. Low risk (post-merge
    local commits without a push are unusual for this workflow).
    """

    def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                args, capture_output=True, text=True, cwd=worktree_path
            )
        except OSError:
            return None

    status = _run(["git", "status", "--porcelain"])
    if status is None or status.returncode != 0:
        return False, "git-error"
    if status.stdout.strip():
        return False, "dirty"

    symbolic_ref = _run(["git", "symbolic-ref", "--short", "HEAD"])
    if symbolic_ref is None or symbolic_ref.returncode != 0:
        return False, "detached-head"
    localname = symbolic_ref.stdout.strip()
    if not localname:
        return False, "detached-head"

    remote_cfg = _run(["git", "config", "--get", f"branch.{localname}.remote"])
    if remote_cfg is None or remote_cfg.returncode != 0:
        return False, "no-upstream-config"
    if remote_cfg.stdout.strip() != "origin":
        return False, "wrong-remote"

    merge_cfg = _run(["git", "config", "--get", f"branch.{localname}.merge"])
    if merge_cfg is None or merge_cfg.returncode != 0:
        return False, "no-upstream-config"
    merge_ref = merge_cfg.stdout.strip()
    if not merge_ref:
        return False, "no-upstream-config"
    pushed = merge_ref.removeprefix("refs/heads/")

    if merged_heads is None:
        return False, "gh-unavailable"
    if pushed not in merged_heads:
        return False, "no-merged-pr"

    return True, "reclaimable"


def enrich_unlocked(worktrees: list[WorktreeInfo], merged_heads: set[str] | None) -> None:
    """Fill in reclaimable + reclaim_reason for unlocked worktrees in-place."""
    for wt in worktrees:
        if not wt.locked:
            wt.reclaimable, wt.reclaim_reason = classify_unlocked_reclaimability(
                wt.path, merged_heads
            )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_worktree(wt: WorktreeInfo, *, dry_run: bool) -> tuple[bool, str]:
    """
    Attempt to remove a stale worktree.

    Steps:
    1. Remove .git/worktrees/<name>/locked so git stops treating it as locked.
    2. `git worktree remove -f <path>` — lets git clean metadata + physical dir.
    3. Fallback: rm -rf physical dir + `git branch -D <branch>`.
    4. `git worktree prune` always at the end.

    Returns (success, message).
    """
    locked_file = REPO_ROOT / ".git" / "worktrees" / wt.name / "locked"

    if dry_run:
        steps = []
        if locked_file.exists():
            steps.append(f"rm {locked_file}")
        steps.append(f"git worktree remove -f {wt.path}")
        steps.append("git worktree prune")
        return True, "would: " + " && ".join(steps)

    # Step 1: unlock
    if locked_file.exists():
        try:
            locked_file.unlink()
        except OSError as exc:
            return False, f"failed to remove lock file: {exc}"

    # Step 2: git worktree remove -f
    removed_by_git = False
    try:
        subprocess.run(
            ["git", "worktree", "remove", "-f", str(wt.path)],
            capture_output=True,
            check=True,
            cwd=REPO_ROOT,
        )
        removed_by_git = True
    except subprocess.CalledProcessError as exc:
        err_msg = exc.stderr.decode(errors="replace").strip() if exc.stderr else str(exc)
        # Fall through to physical removal
        _ = err_msg  # available for debugging if needed

    # Step 3: fallback physical removal
    if not removed_by_git:
        fallback_errors: list[str] = []
        if wt.path.exists():
            try:
                shutil.rmtree(wt.path)
            except OSError as exc:
                fallback_errors.append(f"rmtree: {exc}")
        # Remove git metadata dir
        git_meta = REPO_ROOT / ".git" / "worktrees" / wt.name
        if git_meta.exists():
            try:
                shutil.rmtree(git_meta)
            except OSError as exc:
                fallback_errors.append(f"rmtree git meta: {exc}")
        # Remove branch
        try:
            subprocess.run(
                ["git", "branch", "-D", wt.branch],
                capture_output=True,
                check=True,
                cwd=REPO_ROOT,
            )
        except subprocess.CalledProcessError:
            pass  # branch may already be gone
        if fallback_errors:
            return False, "; ".join(fallback_errors)

    # Step 4: prune
    subprocess.run(["git", "worktree", "prune"], capture_output=True, cwd=REPO_ROOT)

    return True, "removed"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def print_list(worktrees: list[WorktreeInfo], candidates: list[WorktreeInfo]) -> None:
    candidate_names = {wt.name for wt in candidates}
    alive_count = sum(1 for wt in worktrees if wt.alive is True)
    stale_count = sum(1 for wt in worktrees if wt.stale)
    no_pid_count = sum(1 for wt in worktrees if wt.locked and wt.pid is None)
    unlocked = [wt for wt in worktrees if not wt.locked]
    reclaimable = [wt for wt in unlocked if wt.reclaimable]
    kept_unlocked = [wt for wt in unlocked if not wt.reclaimable]
    kept_reasons: dict[str, int] = {}
    for wt in kept_unlocked:
        reason = wt.reclaim_reason or "not-evaluated"
        kept_reasons[reason] = kept_reasons.get(reason, 0) + 1

    print(f"\nFound {len(worktrees)} agent worktrees:")
    for wt in worktrees:
        if wt.stale:
            pid_str = f"pid {wt.pid}, dead" if wt.pid else "no pid"
            print(f"  WARNING  {wt.name} ({pid_str}) — STALE")
        elif wt.alive:
            print(f"  OK  {wt.name} (pid {wt.pid}, alive) — KEEP")
        elif not wt.locked:
            if wt.reclaimable:
                candidate_str = " (candidate under --force)" if wt.name in candidate_names else ""
                print(f"  RECLAIM  {wt.name} (unlocked, merged+clean){candidate_str}")
            else:
                print(f"  -   {wt.name} (unlocked, {wt.reclaim_reason}) — KEEP")
        else:
            pid_str = f"pid {wt.pid}" if wt.pid else "no pid"
            print(f"  ?   {wt.name} ({pid_str}) — KEEP (uncertain)")

    print()
    print("Summary:")
    print(f"  alive:               {alive_count} (keep)")
    if no_pid_count:
        print(f"  no-pid:              {no_pid_count} (keep — cannot determine status)")
    print(f"  stale:               {stale_count} (cleanup candidates)")
    print(f"  unlocked total:      {len(unlocked)}")
    print(f"  unlocked-reclaimable: {len(reclaimable)} (merged+clean — --force candidates)")
    print(f"  unlocked-kept:       {len(kept_unlocked)}")
    for reason, count in sorted(kept_reasons.items()):
        print(f"    - {reason}: {count}")


def output_json(worktrees: list[WorktreeInfo], candidates: list[WorktreeInfo]) -> None:
    candidate_names = {wt.name for wt in candidates}
    data = {
        "total": len(worktrees),
        "stale": sum(1 for wt in worktrees if wt.stale),
        "unlocked_reclaimable": sum(1 for wt in worktrees if not wt.locked and wt.reclaimable),
        "worktrees": [
            {
                "name": wt.name,
                "path": str(wt.path),
                "branch": wt.branch,
                "locked": wt.locked,
                "pid": wt.pid,
                "alive": wt.alive,
                "reclaimable": wt.reclaimable,
                "reclaim_reason": wt.reclaim_reason,
                "candidate": wt.name in candidate_names,
                "status": wt.status_label,
            }
            for wt in worktrees
        ],
    }
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_candidates(
    worktrees: list[WorktreeInfo],
    *,
    include_alive: bool,
    keep_recent: int,
    include_dirty: bool = False,
) -> list[WorktreeInfo]:
    """Filter worktrees down to cleanup candidates.

    Two independent axes compose:
      - locked axis: stale (dead-pid) by default, or all locked worktrees
        (including alive) if `include_alive` (DANGEROUS).
      - unlocked axis: merged+clean unlocked worktrees (`reclaimable`) by
        default, plus ALL non-reclaimable unlocked worktrees (dirty /
        unmerged / unpushed / uncertain) if `include_dirty` (DANGEROUS —
        may destroy uncommitted/unpushed work; requires `reclaimable` to
        have been computed via `enrich_unlocked` beforehand).
    """
    # Locked axis: stale (dead-pid locked) — or all locked if --include-alive
    if include_alive:
        candidates = [wt for wt in worktrees if wt.locked]
    else:
        candidates = [wt for wt in worktrees if wt.stale]

    # Unlocked axis: merged+clean reclaimable worktrees are always safe to add
    candidates += [wt for wt in worktrees if not wt.locked and wt.reclaimable]

    # --include-dirty: additionally target non-reclaimable unlocked worktrees
    if include_dirty:
        candidates += [wt for wt in worktrees if not wt.locked and not wt.reclaimable]

    # --keep-recent N: retain the last N worktrees by list order (as-is from git)
    if keep_recent > 0 and keep_recent < len(worktrees):
        recent_names = {wt.name for wt in worktrees[-keep_recent:]}
        candidates = [wt for wt in candidates if wt.name not in recent_names]

    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cleanup stale Claude agent worktrees with dead PIDs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list",
        action="store_true",
        default=False,
        help="Show what would be cleaned (default mode, no changes).",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate cleanup — print actions without executing.",
    )
    mode.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Actually remove stale worktrees.",
    )
    parser.add_argument(
        "--keep-recent",
        type=int,
        default=0,
        metavar="N",
        help="Keep the most recent N worktrees regardless of status.",
    )
    parser.add_argument(
        "--include-alive",
        action="store_true",
        default=False,
        help="DANGEROUS: also target worktrees with living processes.",
    )
    parser.add_argument(
        "--include-dirty",
        action="store_true",
        default=False,
        help=(
            "DANGEROUS: also target unlocked worktrees that are NOT proven "
            "merged+clean (dirty, unmerged, unpushed, or uncertain). May "
            "destroy uncommitted or unpushed work."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Machine-readable JSON output.",
    )

    args = parser.parse_args(argv)

    # Default to --list if no mode specified
    if not args.dry_run and not args.force:
        args.list = True

    if args.include_alive and not (args.dry_run or args.force):
        print("WARNING: --include-alive has no effect without --dry-run or --force.")
    if args.include_dirty and not (args.dry_run or args.force):
        print("WARNING: --include-dirty has no effect without --dry-run or --force.")

    # --- Gather info ---
    worktrees = get_agent_worktrees()
    if not worktrees:
        print("No agent worktrees found.")
        return 0

    enrich(worktrees)

    # Fetch the merged-PR head set once (not per-worktree), only if needed.
    if any(not wt.locked for wt in worktrees):
        merged_heads = fetch_merged_head_set()
        if merged_heads is None:
            print(
                "WARNING: gh unavailable — cannot confirm merges, "
                "keeping all unlocked worktrees.",
                file=sys.stderr,
            )
        enrich_unlocked(worktrees, merged_heads)

    candidates = build_candidates(
        worktrees,
        include_alive=args.include_alive,
        keep_recent=args.keep_recent,
        include_dirty=args.include_dirty,
    )

    # --- Output ---
    if args.json:
        output_json(worktrees, candidates)
        return 0

    if args.list:
        print_list(worktrees, candidates)
        return 0

    # dry-run or force
    if args.include_alive:
        print("WARNING: --include-alive is set — alive-process worktrees will be targeted!")
    if args.include_dirty:
        print(
            "WARNING: --include-dirty is set — dirty/unmerged/unpushed "
            "unlocked worktrees will be targeted!"
        )

    mode_label = "Simulating cleanup" if args.dry_run else "Cleaning up"
    print(f"\n{mode_label} of {len(candidates)} worktrees...")

    cleaned = 0
    failed = 0
    for wt in candidates:
        success, msg = cleanup_worktree(wt, dry_run=args.dry_run)
        marker = "OK" if success else "FAIL"
        print(f"  {marker}  {wt.name} — {msg}")
        if success:
            cleaned += 1
        else:
            failed += 1

    print()
    label = "Would clean" if args.dry_run else "Cleaned"
    print(f"{label}: {cleaned} / {len(candidates)}")
    if failed:
        print(f"Failed:  {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
