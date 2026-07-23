# Agent Worktree Cleanup (`scripts/cleanup_agent_worktrees.py`)

A garbage-collector for subagent worktrees — removes stale entries that
accumulate when subagents exit without cleaning up their own worktrees, and
that `git worktree remove --force` alone cannot remove due to stale lock files.

## Why

Each subagent in a parallel dispatch session receives an isolated git worktree
under `.claude/worktrees/agent-*`. When a session ends normally the worktrees
are removed. When a session is interrupted — by a crash, a timeout, or a
manual kill — the worktrees and their lock files remain on disk.

Over multiple sessions this accumulates quickly. 145+ orphaned worktrees were
observed in practice after a heavy dispatch day. The problem is not just disk
usage: `git worktree remove --force` still refuses to remove worktrees with
lock files (the force flag bypasses modified-file checks, not lock files). The
cleanup script reads the lock reason, extracts the PID, and skips the worktree
if the PID is still alive — otherwise it deletes the lock file first, then
calls `git worktree remove -f`.

## Setup

Works on macOS and Linux. No installation required beyond the project's
standard dependencies:

```bash
python scripts/cleanup_agent_worktrees.py [flags]
```

The script must be run from a directory inside the git repository it is
cleaning up. It uses `git worktree list` relative to the working directory.

## Usage

### `--list` — inspect candidates (default)

```bash
python scripts/cleanup_agent_worktrees.py
# or equivalently
python scripts/cleanup_agent_worktrees.py --list
```

Lists all worktrees matching `agent-*` under `.claude/worktrees/`, annotated
with their lock status and whether the lock PID is alive or dead. Unlocked
worktrees are further classified as reclaimable (merged+clean — will be
removed by `--force`) or kept, with a reason (dirty / no-merged-PR /
no-upstream-config / wrong-remote / detached-head / gh-unavailable /
git-error). No changes are made.

Example output:

```
Worktree candidates (agent-* only):

  [DEAD]  .claude/worktrees/agent-a1b2c3d4  locked by pid=12345 (dead)
  [DEAD]  .claude/worktrees/agent-e5f6a7b8  locked by pid=67890 (dead)
  [LIVE]  .claude/worktrees/agent-c9d0e1f2  locked by pid=11111 (alive)
  [UNLOCKED]  .claude/worktrees/agent-g3h4i5j6

4 worktrees found: 2 dead, 1 alive, 1 unlocked
```

Use this before any destructive operation to verify which worktrees will be
affected.

### `--dry-run` — simulate cleanup

```bash
python scripts/cleanup_agent_worktrees.py --dry-run
```

Shows exactly what would be removed without making any changes. Output format
matches `--force`, with each action prefixed by `[DRY RUN]`. Exit code 0
always (no changes to check).

### `--force` — remove dead worktrees, plus merged+clean unlocked worktrees

```bash
python scripts/cleanup_agent_worktrees.py --force
```

Removes all worktrees with dead PIDs — a worktree must be locked with a
PID that is no longer alive to qualify. For each candidate:
1. Deletes the `.git/worktrees/<id>/locked` file (if present)
2. Calls `git worktree remove -f <path>`

Alive-PID worktrees are left untouched. Unlocked worktrees are removed only
when proven **merged and clean** — see [Unlocked-worktree clean-gate
reclaim](#unlocked-worktree-clean-gate-reclaim-force-3237) below for the
exact safety rule. Any unlocked worktree that doesn't clear that bar (dirty,
unmerged, unpushed, or uncertain) is left untouched by `--force` alone.

Example output:

```
Removing: .claude/worktrees/agent-a1b2c3d4  [dead pid=12345]
  deleted lock file
  git worktree remove -f: OK
Removing: .claude/worktrees/agent-e5f6a7b8  [dead pid=67890]
  deleted lock file
  git worktree remove -f: OK
Skipping: .claude/worktrees/agent-c9d0e1f2  [alive pid=11111]

Removed 2 worktrees, skipped 1 (alive)
```

### Unlocked-worktree clean-gate reclaim (`--force`, #3237)

`--force` also reclaims UNLOCKED worktrees when they are proven safe — merged
and clean. reyn merges via **squash-merge + branch-delete**, so a worktree's
original commits are never ancestors of `origin/main`: `git branch -r
--contains HEAD` is always empty for a squash-merged worktree, and
`git rev-parse HEAD@{upstream}` **errors** once the remote branch is deleted
and pruned (`fatal: ambiguous argument '...@{upstream}': unknown revision`).
Neither signal can be used.

An unlocked worktree is **reclaimable** iff ALL of:

1. `git status --porcelain` is empty (no uncommitted or untracked changes)
2. it was pushed to `origin`, and its pushed branch has a **merged PR** —
   keyed via `branch.<local-branch>.merge` git config, NOT `@{upstream}`.
   This config is a pure local read that **survives remote-ref pruning**,
   so it still resolves the pushed branch name after the remote branch is
   deleted. The merged-PR head set is fetched once (not per-worktree) via
   `gh pr list --state merged --limit 5000 --json headRefName`.

If ANY of the two fails, the worktree is **kept**. This includes: a git
command erroring, a detached HEAD (no `symbolic-ref`), a missing
`branch.<name>.merge`/`.remote` config (push never set upstream), a remote
other than `origin`, or the branch simply not (yet) having a merged PR.

**`git stash` is deliberately NOT part of this gate.** The stash ref lives
in the shared `.git` dir, not per-worktree — every worktree of a repo
(including `main`) sees the SAME stash list, so it is not evidence of any
one worktree's state. It also survives `git worktree remove` (it isn't
stored in the worktree), so it was never a reclaim-time loss vector to
protect against.

**Fail-safe on `gh` unavailability.** If `gh pr list` fails for any reason —
offline, not authenticated, API error — the merged-PR set is unavailable and
**every unlocked worktree is kept**, unconditionally. The script never
guesses at merge status.

**Known residual (documented, not solved).** Unpushed commits made *after* a
merge (rare — committing to an already-merged worktree without pushing) are
not captured by this signal: such a worktree would read as clean + merged
yet hold unpushed work. This is low risk for the per-PR-coder workflow and is
called out here rather than solved.

### `--include-dirty` — also reclaim non-reclaimable unlocked worktrees (DANGEROUS)

```bash
python scripts/cleanup_agent_worktrees.py --force --include-dirty
```

Additionally targets unlocked worktrees that did **not** pass the
reclaimable check above — dirty, unmerged, unpushed, or otherwise uncertain.
**May destroy uncommitted or unpushed work.** Mirrors `--include-alive`:
provided for deliberate bulk recovery, not routine cleanup.

### `--keep-recent N` — preserve the N most recently modified worktrees

```bash
python scripts/cleanup_agent_worktrees.py --force --keep-recent 5
```

Sorts candidates by last-modified time and exempts the N most recent from
removal, regardless of lock status. Useful when you want to preserve the
outputs of the most recent dispatch batch for inspection:

```bash
# Clean up everything older than the last 3 runs
python scripts/cleanup_agent_worktrees.py --force --keep-recent 3
```

### `--include-alive` — also remove alive worktrees (DANGEROUS)

```bash
python scripts/cleanup_agent_worktrees.py --force --include-alive
```

Removes worktrees even when their lock PID is alive. This terminates any
subagent that is currently using the worktree.

**Use only when:**
- You are certain the owning process is a zombie (PID exists in the process
  table but the process is not actually running)
- You are deliberately terminating a stuck session

Default behavior (alive = keep) exists for safety. `--include-alive` is
provided for rare recovery scenarios and should not be part of routine cleanup.

### `--json` — machine-readable output

```bash
python scripts/cleanup_agent_worktrees.py --list --json
python scripts/cleanup_agent_worktrees.py --force --json
```

Emits a JSON object with a `worktrees` array (each entry annotated with
`reclaimable` and `reclaim_reason` for unlocked worktrees) plus summary
counts including `unlocked_reclaimable`. Useful for piping into `jq` for
custom filtering or for CI scripts that need structured output.

## Flag reference

| Flag | Default | Description |
|------|---------|-------------|
| `--list` | on | List candidates and their status; no changes made |
| `--dry-run` | off | Simulate removal; print what would happen |
| `--force` | off | Remove dead-PID (stale) worktrees, plus merged+clean unlocked worktrees |
| `--keep-recent N` | 0 (keep none) | Exempt the N most recently modified worktrees |
| `--include-alive` | off | Also remove alive-PID worktrees (dangerous) |
| `--include-dirty` | off | Also remove unlocked worktrees that are dirty/unmerged/unpushed (dangerous) |
| `--json` | off | Machine-readable JSON output |

## Integration with workflow

### After a heavy parallel dispatch session

Run `--list` first to see how many orphans accumulated, then `--force` to
remove them:

```bash
python scripts/cleanup_agent_worktrees.py --list
python scripts/cleanup_agent_worktrees.py --force
```

### Periodic cleanup with a retain window

Keep the most recent 5 worktrees for post-session inspection while removing
everything older:

```bash
python scripts/cleanup_agent_worktrees.py --force --keep-recent 5
```

### CI stale worktree detection

In CI, use `--list --json` to detect accumulated worktrees and alert when the
count exceeds a threshold, without removing anything automatically:

```bash
count=$(python scripts/cleanup_agent_worktrees.py --list --json | \
  jq -r 'select(.status == "dead") | .path' | wc -l)
if [ "$count" -gt 20 ]; then
  echo "Warning: $count stale worktrees detected"
fi
```

### When NOT to use this tool

- **Removing non-agent worktrees.** The script filters on `agent-*` prefix
  under `.claude/worktrees/`. It deliberately ignores all other worktrees.
  Do not modify the filter to broaden scope without understanding the safety
  implications.
- **Recovering from a bad dispatch.** If a subagent produced incorrect results
  you want to inspect, run `--list` first and confirm the worktree's status
  before removing it. An unlocked worktree is only removed by `--force` if it
  is proven merged+clean (see [Unlocked-worktree clean-gate
  reclaim](#unlocked-worktree-clean-gate-reclaim-force-3237) above); a
  dirty or unmerged worktree you want to inspect is always kept unless you
  pass `--include-dirty`. Use `--keep-recent` to protect recent worktrees
  during cleanup.

## Safety properties

- **Alive PIDs are never touched without `--include-alive`.** The PID check is
  performed via `ps -p <pid>` before any deletion. If the check fails (PID
  alive), the worktree is skipped and logged.
- **`--dry-run` is always safe.** No filesystem or git operations are
  performed.
- **Unlocked worktrees are only removal candidates once proven merged+clean.**
  `build_candidates()` adds unlocked worktrees only when
  `classify_unlocked_reclaimability()` returns `reclaimable=True` (porcelain
  empty, pushed branch has a merged PR — keyed via `branch.<name>.merge`
  config, never `@{upstream}`; `git stash` is deliberately not checked — the
  stash ref is shared across all worktrees of a repo, so it isn't
  per-worktree evidence, and it survives `git worktree remove` regardless).
  Any uncertainty —
  dirty tree, no upstream config, wrong remote, detached HEAD, `gh`
  unavailable, git error — resolves to KEEP. `--include-dirty` is the only
  way to widen this to non-reclaimable unlocked worktrees, and it is
  DANGEROUS.
- **The script targets only `agent-*` paths under `.claude/worktrees/`.** All
  other worktrees — feature branches, main, etc. — are invisible to it.

## See also

- [LLM Payload Tracing](dogfood-tracing.md) — complementary debug tooling for
  inspecting LLM payloads from parallel dispatch sessions
