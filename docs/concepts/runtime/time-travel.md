---
type: concept
topic: architecture
audience: [human, agent]
---

# Time-Travel: Rewind and Resume

Reyn's time-travel system lets you rewind the agent to any past checkpoint and
optionally branch from there. It is a separate feature from [crash recovery](../../concepts/skills/skill-resume.md) — the two use different mechanisms and serve different purposes.

> **Crash recovery** (`ReplayEngine` / WAL) automatically restores the agent after an unexpected failure — it is transparent to the user and replays forward to where the run stopped. **Time-travel** is an intentional, user-initiated rewind to an earlier point, with the option to fork a new branch of history.

## Concepts

### Checkpoints

A checkpoint marks a boundary between agent states. Reyn creates checkpoints at:

- **Turn boundaries** — after the LLM finishes its response turn.
- **Plan-step boundaries** — after each plan step completes.
- **Phase boundaries** — after each OS phase transition within a skill run.

Each checkpoint has a monotonically increasing **sequence number** (seq). The seq is the global clock used to address all time-travel operations.

### Rewind

`/rewind` rewinds the agent to a past checkpoint. Both substrates are rewound atomically:

1. **Runtime substrate** — the agent's conversation state and skill-run execution memo are restored to the snapshot at the target seq.
2. **Workspace substrate** — the workspace files are restored to the shadow-git `as-of-N` state at that seq.

The rewind is **atomic**: either both substrates revert or neither does. There is no intermediate state where the conversation is at seq K while the files are at seq K+5.

### Append-only history

Reyn never rewrites history. When you rewind to seq R:

- A **reset-record** is appended at seq R on the current branch, marking it as the new tip.
- The commit history before R is preserved.
- Conversation turns and workspace states beyond R become **abandoned** on the current branch — they are accessible via the branch tree, not erased.

This means rewind is always recoverable: you can navigate back to an abandoned future by switching branches.

### Branching

After a rewind you can fork from that point instead of overwriting the current branch:

- **Undo (active-branch checkout)** — rewinding within the current branch; the branch timeline moves back to seq R.
- **Fork-switch (inactive-branch checkout)** — switching to an existing abandoned branch at a different seq. The branch registry tracks all branches by lineage.

The `checkout(seq)` primitive implements both: if the target seq is on the active branch it is an undo; if it is on an abandoned branch it is a fork-switch.

### Act-turn rewind

For finer granularity, within a live skill run you can rewind to an **act-turn boundary** (a step within the current turn) without touching the workspace. This is a runtime-only operation using the Ghost-Replay mechanism: the committed-step memo is truncated at the target seq, and on relaunch steps before the target replay as ghosts (0 tokens, recorded results replayed), while steps after the target re-execute. The workspace reflects the last boundary generation, not mid-step file state.

---

## Architecture

### PITR snapshot and WAL-diff reconstruct

Each checkpoint stores an **AgentSnapshot** — a point-in-time snapshot of the agent's conversation state (inbox, message history, plan state). At rewind time:

1. The snapshot at or before the target seq is located.
2. The WAL (`StateLog`, `.reyn/state/wal.jsonl`) events between the snapshot and the target seq are applied as a diff — replaying only the delta, not the full history.

The WAL is a **synchronous-durability log** (fsync'd per append), separate from the P6 audit event log. See [Events](events.md) for the WAL vs audit-event distinction.

### Global single-seq WAL and consistent-cut

All WAL events share a **global single sequence namespace**. A consistent-cut rewind at seq N is well-defined: "the state of every substrate at the moment before seq N+1 was written". The global seq makes the cut precise — there is no per-substrate clock to reconcile.

### Append-only reset-record and branch state

When rewind commits, a **reset-record** is written to the WAL at the target seq. The branch registry derives branch state from this chain: a seq between a reset-record at R and the next reset-record (or current tip) belongs to the interval `[R, next_R)`. Abandoned intervals — seqs that were reachable before a rewind but are now beyond the current tip — form the inactive branches in the branch tree.

### Branch registry

The branch registry tracks all fork lineages: when a fork-switch creates a new branch from an abandoned interval, the registry records the origin seq and the new branch identity. The picker UI reads from the registry to render the tree view.

### checkout primitive

`checkout(seq)` is the unified primitive:

- If seq is on the **active branch** (seq ≤ current tip): undo — rewinds the active branch to seq.
- If seq is on an **inactive branch** (in an abandoned interval): fork-switch — activates that branch at seq, leaving the current branch as a new abandoned interval.

The implementation uses the `rewind_to` path with a minus-guard to prevent rewinding past the branch's own origin.

### Shadow-git workspace versioning

The workspace substrate uses a **shadow git repository** for content-addressed versioning. At each boundary seq, the current file tree is committed as a shadow-git generation. Rewinding to seq N restores the workspace to the shadow-git commit corresponding to the largest generation ≤ N. This makes workspace rewind O(changed files) rather than a full replay of all file operations.

Container-mode: when the container environment backend is active, the shadow-git `as-of-N` restore operates inside the container filesystem.

### Act-turn Ghost-Replay

Act-turn rewind (intra-turn granularity) uses `SkillResumeCoordinator.plan_for_act_turn_rewind`: the `SkillResumeAnalyzer` builds the full `ResumePlan`, then `committed_steps` are filtered to `seq ≤ target_seq`. On relaunch via `OSRuntime.run(resume_plan=...)`, steps in the memo replay as ghosts (0 LLM tokens); steps beyond the cutoff re-execute. This reuses the existing crash-resume dispatch path — no new runtime wiring.

### 2-substrate generation at boundary seq

At every checkpoint boundary, Reyn generates a **paired generation**:

```
AgentSnapshot  (runtime substrate)
    ⊗
shadow-git commit  (workspace substrate)
    at  boundary seq N
```

This pair is the unit that `checkout(seq)` restores atomically. The pairing is by seq: the snapshot and the shadow-git commit carry the same seq tag, so rewind can locate both with one lookup.

### WAL vs audit-event separation

The WAL (`StateLog`, `.reyn/state/wal.jsonl`) and the P6 audit event log (`EventStore`, `.reyn/events/<run_id>.jsonl`) are **separate logs with different contracts**:

| | WAL (StateLog) | Audit log (EventStore) |
|--|--|--|
| Purpose | Crash recovery and time-travel reconstruct | Audit trail and replay |
| Durability | fsync per append (synchronous) | Rotation-based (not per-append fsync) |
| Lifecycle | Truncatable after snapshot | Append-only, rotation-based |
| Unification | **Prohibited** — sync durability requirements differ | — |

Do not conflate or merge the two logs. See [Events](events.md) for details.

---

## Relationship to crash recovery

| | Crash recovery | Time-travel |
|--|--|--|
| Trigger | Automatic on unexpected failure | User-initiated (`/rewind`) |
| Direction | Forward-replay to resume | Backward to a past checkpoint |
| Workspace | Not rewound | Rewound to as-of-N |
| Branching | None | Fork / branch tree |
| Mechanism | `SkillResumeAnalyzer` + WAL forward-replay | PITR snapshot + WAL-diff + shadow-git |
| Design | [ADR-0002](../../deep-dives/decisions/) | [ADR-0038 draft](https://github.com/tya5/reyn/pull/1536) |

## See also

- [Crash Recovery / Skill Resume](../../concepts/skills/skill-resume.md)
- [Events and the WAL](events.md)
- [How to use rewind](../../guide/for-users/time-travel.md)
