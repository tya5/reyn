#!/usr/bin/env python3
"""Cleanup stale agent worktrees from .claude/worktrees/agent-* paths.

Detects worktrees whose lock file references a dead PID and removes them
safely, skipping any whose process is still alive.

Usage:
    python scripts/cleanup_agent_worktrees.py --list        # default: show breakdown
    python scripts/cleanup_agent_worktrees.py --dry-run     # simulate cleanup
    python scripts/cleanup_agent_worktrees.py --force       # actually remove stale
    python scripts/cleanup_agent_worktrees.py --keep-recent 5 --force
    python scripts/cleanup_agent_worktrees.py --include-alive --dry-run  # dangerous
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
    stale_count = len(candidates)
    unlocked_count = sum(1 for wt in worktrees if not wt.locked)
    no_pid_count = sum(1 for wt in worktrees if wt.locked and wt.pid is None)

    print(f"\nFound {len(worktrees)} agent worktrees:")
    for wt in worktrees:
        if wt.name in candidate_names:
            pid_str = f"pid {wt.pid}, dead" if wt.pid else "no pid"
            print(f"  WARNING  {wt.name} ({pid_str}) — STALE")
        elif wt.alive:
            print(f"  OK  {wt.name} (pid {wt.pid}, alive) — KEEP")
        elif not wt.locked:
            print(f"  -   {wt.name} (unlocked) — KEEP")
        else:
            pid_str = f"pid {wt.pid}" if wt.pid else "no pid"
            print(f"  ?   {wt.name} ({pid_str}) — KEEP (uncertain)")

    print()
    print("Summary:")
    print(f"  alive:    {alive_count} (keep)")
    if unlocked_count:
        print(f"  unlocked: {unlocked_count} (keep)")
    if no_pid_count:
        print(f"  no-pid:   {no_pid_count} (keep — cannot determine status)")
    print(f"  stale:    {stale_count} (cleanup candidates)")


def output_json(worktrees: list[WorktreeInfo], candidates: list[WorktreeInfo]) -> None:
    candidate_names = {wt.name for wt in candidates}
    data = {
        "total": len(worktrees),
        "stale": len(candidates),
        "worktrees": [
            {
                "name": wt.name,
                "path": str(wt.path),
                "branch": wt.branch,
                "locked": wt.locked,
                "pid": wt.pid,
                "alive": wt.alive,
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
) -> list[WorktreeInfo]:
    """Filter worktrees down to cleanup candidates."""
    # Base: stale (dead-pid locked) — or alive if --include-alive is set
    if include_alive:
        candidates = [wt for wt in worktrees if wt.locked]
    else:
        candidates = [wt for wt in worktrees if wt.stale]

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

    # --- Gather info ---
    worktrees = get_agent_worktrees()
    if not worktrees:
        print("No agent worktrees found.")
        return 0

    enrich(worktrees)
    candidates = build_candidates(
        worktrees,
        include_alive=args.include_alive,
        keep_recent=args.keep_recent,
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

    mode_label = "Simulating cleanup" if args.dry_run else "Cleaning up"
    print(f"\n{mode_label} of {len(candidates)} stale worktrees...")

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
