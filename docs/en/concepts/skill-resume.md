# Skill resume

How Reyn restores in-flight skill execution after a process crash.

## What gets restored

When a skill is mid-execution and the Reyn process dies (kill -9, OOM,
machine reboot, etc.), the next `reyn chat` startup automatically:

1. Loads the per-agent snapshot (`AgentSnapshot.load`)
2. Replays the WAL forward to the latest known state
3. Per skill that was in-flight:
   - Loads the per-skill snapshot
   - Builds a `ResumePlan` from snapshot + WAL events
   - Decides an action via `SkillResumeCoordinator` (default: skip
     ambiguous side-effect ops, resume from the in-flight phase)
4. Resumes each active skill from its `current_phase` (fast-forward)

The completed phases are NOT re-executed. Within the in-flight phase,
already-committed ops are memoized (results loaded from WAL, not
re-invoked), and LLM calls within that phase are also memoized so
resume does not re-pay LLM cost.

## What's preserved across crashes

| State | Where | Survives crash |
|---|---|---|
| Workspace artifacts | `.reyn/agents/<name>/workspace/` | yes |
| Per-agent state (inbox, chains, interventions) | `agents/<name>/state/snapshot.json` | yes |
| Per-skill state (current phase, visit counts) | `agents/<name>/state/skills/<run_id>.snapshot.json` | yes |
| WAL (committed ops + LLM responses) | `.reyn/state/wal.jsonl` | yes |
| Active asyncio.Tasks | in-memory only | no — resumes via fresh tasks on restart |

## Ambiguous steps and resume policy

A "side-effect" op (e.g. `mcp/call_tool` writing externally,
`file/write`, `shell`) emits `step_started` to the WAL before invoking
the underlying call. If the process crashes after `step_started` but
before `step_completed`, the resume system can't tell whether the side
effect actually happened — the op is **ambiguous**.

The Coordinator applies the resume policy from `reyn.yaml`:

```yaml
skill_resume:
  default: skip            # default: assume ambiguous step completed
  per_skill:
    blog_publisher: prompt # case-by-case interactive prompt (planned)
    eval_runner: skip      # idempotent reads — skip is safe
```

Policy values:

- `skip` — treat ambiguous step as completed with empty result. Safe
  default; prevents duplicate side effects.
- `retry` — re-invoke the op (accept the duplicate-side-effect risk)
- `discard_skill` — abort the whole skill_run
- `prompt` — bulk-prompt the user at startup (R-D3, separate PR)

## Manual control

If you need to manage individual runs:

```
/skill list                  # show active skill runs
/skill discard <run_id>      # abort one specific run
```

For starting fresh:

```bash
reyn chat --no-restore       # skip restore this run (state stays on disk)
reyn chat --reset            # wipe in-flight skill state (with confirm)
```

## See also

- [Upgrade policy](../reference/upgrade-policy.md) — schema version
  refuses and the `--reset` remediation
- [Permission model](permission-model.md) — what counts as a side effect
- [Events](events.md) — WAL + audit log architecture
