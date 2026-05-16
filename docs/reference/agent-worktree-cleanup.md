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
with their lock status and whether the lock PID is alive or dead. No changes
are made.

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

### `--force` — remove dead worktrees

```bash
python scripts/cleanup_agent_worktrees.py --force
```

Removes all worktrees with dead PIDs (or no lock file). For each candidate:
1. Deletes the `.git/worktrees/<id>/locked` file (if present)
2. Calls `git worktree remove -f <path>`

Alive-PID worktrees are left untouched. Unlocked worktrees (no lock file) are
also removed by default — they have no running process protecting them.

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

Emits one JSON object per worktree to stdout, followed by a summary object.
Useful for piping into `jq` for custom filtering or for CI scripts that need
structured output:

```json
{"path": ".claude/worktrees/agent-a1b2c3d4", "status": "dead", "pid": 12345, "action": "removed"}
{"path": ".claude/worktrees/agent-c9d0e1f2", "status": "alive", "pid": 11111, "action": "skipped"}
{"summary": {"total": 2, "removed": 1, "skipped": 1}}
```

## Flag reference

| Flag | Default | Description |
|------|---------|-------------|
| `--list` | on | List candidates and their status; no changes made |
| `--dry-run` | off | Simulate removal; print what would happen |
| `--force` | off | Remove dead-PID and unlocked worktrees |
| `--keep-recent N` | 0 (keep none) | Exempt the N most recently modified worktrees |
| `--include-alive` | off | Also remove alive-PID worktrees (dangerous) |
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
  you want to inspect, run `--list` first and confirm the worktree is in the
  dead/unlocked list before removing it. Use `--keep-recent` to protect
  recent worktrees during cleanup.

## Safety properties

- **Alive PIDs are never touched without `--include-alive`.** The PID check is
  performed via `ps -p <pid>` before any deletion. If the check fails (PID
  alive), the worktree is skipped and logged.
- **`--dry-run` is always safe.** No filesystem or git operations are
  performed.
- **The script targets only `agent-*` paths under `.claude/worktrees/`.** All
  other worktrees — feature branches, main, etc. — are invisible to it.

## See also

- [LLM Payload Tracing](dogfood-tracing.md) — complementary debug tooling for
  inspecting LLM payloads from parallel dispatch sessions
