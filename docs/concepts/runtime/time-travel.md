---
type: concept
topic: architecture
audience: [human, agent]
---

# Time-Travel: Rewind and Resume

Reyn's time-travel system lets you rewind the agent to any past checkpoint and
optionally branch from there. It is a separate feature from crash recovery — the two use different mechanisms and serve different purposes.

> **Crash recovery** (WAL + snapshot reconstruction) automatically restores the agent after an unexpected failure — state is rebuilt from the WAL (`.reyn/state/wal.jsonl`) plus seq-keyed snapshots, transparent to the user, replaying forward to where the run stopped. **Time-travel** is an intentional, user-initiated rewind to an earlier point, with the option to fork a new branch of history.

## Concepts

### Checkpoints

A checkpoint marks a boundary between agent states. Reyn creates checkpoints at:

- **Turn boundaries** — after the LLM finishes its response turn.
- **Plan-step boundaries** — after each plan step completes.
- **Phase boundaries** — after each OS phase transition within a workflow run.

Each checkpoint has a monotonically increasing **sequence number** (seq). The seq is the global clock used to address all time-travel operations.

### Rewind

`/rewind` rewinds the agent to a past checkpoint, restoring the **runtime substrate** — the agent's conversation state and workflow-run execution memo — to the snapshot at the target seq.

Reyn time-travels its own `.reyn/` state only. User workspace files remain at HEAD. See [`.reyn/` directory layout](../../reference/runtime/reyn-dir-layout.md#recovery-core) for the full recovery-core classification.

### Append-only history

Reyn never rewrites history. When you rewind to a past checkpoint (seq **N**):

- A reset-record is appended at a **new seq R** (beyond the current tip) carrying target **N**; that record becomes the new tip, and the agent state is restored **as-of-N**.
- History before N is preserved.
- Turns in **`(N, R)`** become **abandoned** on the current branch — reachable via the branch tree, not erased.

This means rewind is always recoverable: you can navigate back to an abandoned future by switching branches.

### Branching

After a rewind you can fork from that point instead of overwriting the current branch:

- **Undo (active-branch checkout)** — rewinding within the current branch; the branch timeline moves back to seq R.
- **Fork-switch (inactive-branch checkout)** — switching to an existing abandoned branch at a different seq. The branch registry tracks all branches by lineage.

The `checkout(seq)` primitive implements both: if the target seq is on the active branch it is an undo; if it is on an abandoned branch it is a fork-switch.

### Act-turn rewind — removed (#4711)

> **Status: removed, not aspirational.** A finer, intra-turn "act-turn boundary"
> rewind granularity (`e41929324`, "act-turn workspace restore") existed and was
> deleted along with the phase-graph skill engine's dispatch machinery
> (`SkillResumeCoordinator`/`OSRuntime` — #2434 stage3b's bulk-delete, `d3c8c7a1e`;
> #2439's follow-up, `068ffcd29`) — confirmed via `git log -S` on both classes,
> zero grep hits in current `src/`. Checkpoint-boundary rewind below is unaffected
> and is the only granularity `/rewind` exposes today — [the operator
> guide](../../guide/for-users/time-travel.md) never described act-turn rewind,
> so no user-facing claim needed correcting.

---

## Architecture

### PITR snapshot and WAL-diff reconstruct

Each checkpoint stores an **AgentSnapshot** — a point-in-time snapshot of the agent's conversation state (inbox, message history, plan state). At rewind time:

1. The snapshot at or before the target seq is located.
2. The WAL (`StateLog`, `.reyn/state/wal.jsonl`) events between the snapshot and the target seq are applied as a diff — replaying only the delta, not the full history.

The WAL is an **fsync-per-append log**: each entry is durably written via `DurabilityWorker` off the task loop. Task-loop writes are fire-and-forget except `step_started`, which blocks until durable (durable-before-side-effect invariant). Recovery restores to the last durable entry; an un-durable tail at crash is a consistent-prefix loss. Separate from the P6 audit event log — see [Events](events.md) for the WAL vs audit-event distinction.

**Snapshot writes share the WAL's off-loop durability path.** `SnapshotJournal.save()` routes its write+fsync through the same `DurabilityWorker` rather than writing inline on the event loop — a slow snapshot write no longer freezes other sessions or the TUI while it completes. Ordering is the crash-consistency invariant that makes this safe: a snapshot's `applied_seq` is made durable only *after* the WAL entry for that seq is already durable, never before or concurrently — so recovery can never observe a snapshot pointing at a WAL seq that isn't actually on disk.

### Global single-seq WAL and consistent-cut

All WAL events share a **global single sequence namespace**. A consistent-cut rewind at seq N is well-defined: "the state of every substrate at the moment before seq N+1 was written". The global seq makes the cut precise — there is no per-substrate clock to reconcile.

The cut is also **process-global across Sessions and Agents**: because there is one WAL, a single reset-record moves *every* loaded [Session](../multi-agent/sessions.md) and Agent to the target seq atomically — rewind is not scoped to one Session. Per-Session granularity lives in **persistence** (snapshots are re-keyed per Session) and crash-recovery replay, not in the rewind operation itself.

### Append-only reset-record and branch state

When rewind commits, a **reset-record** is appended to the WAL at its own new seq **R** (distinct from the rewind target **N**). The record carries `target_n=N`. The open interval `(N, R)` — seqs that existed between the rewind target and the reset-record itself — is marked **abandoned** (dead-branch id=R). Active seqs are the complement: every seq not in any abandoned interval on the current branch chain. The branch registry derives branch state from the chain of reset-records: each record's `target_n` and `id=R` define one abandoned interval.

### Branch registry

The branch registry tracks all fork lineages: when a fork-switch creates a new branch from an abandoned interval, the registry records the origin seq and the new branch identity. The picker UI reads from the registry to render the tree view.

### checkout primitive

`checkout(seq)` is the **core primitive** for all rewind and fork-switch operations. `rewind_to` is a thin wrapper that adds an `is_active` guard around `checkout`.

- If `is_active_seq(seq)` is true: undo — rewinds the current branch to seq.
- If `is_active_seq(seq)` is false (seq is in an abandoned interval): fork-switch — activates the abandoned branch at seq, leaving the current branch tip as a new abandoned interval.

`is_active_seq` is **not** equivalent to `seq ≤ tip` — a seq can be ≤ the current tip but still be in an abandoned interval from a prior rewind. Activeness is derived from the reset-record chain, not from position relative to tip.

**Deferred-purge atomicity (#2125).** Rewinding past a spawned session's spawn point drops that session's on-disk directory as part of reconstruction. The destructive removal is deferred until the as-of-cut reconstruction has actually succeeded — if reconstruction raises partway through, the session directory still exists afterward, not half-torn-down. A failed rewind leaves state intact rather than partially destroyed.

At rewind and fork-switch, the runtime reconstruct honors `is_active` (following the correct fork-lineage path for the target branch).

### Boundary generation

At every checkpoint boundary, Reyn writes an **AgentSnapshot** — the runtime state at that seq (inbox, message history, plan state). There is no workspace commit; the boundary artifact is a single runtime snapshot. `checkout(seq)` locates the snapshot at or before the target seq, then replays WAL events forward to reach the exact target state. Config state is reconstructed from its own generation store (see [`.reyn/` directory layout](../../reference/runtime/reyn-dir-layout.md#recovery-core)).

### Task subscriptions and the backend at rewind

Time-travel rewinds the runtime substrate — the agent's conversation state. Task-related state splits into what lives inside the runtime substrate and what lives in the external backend:

**What gets rewound (Reyn-internal):** The task↔session binding — which session is the `assignee`, which is the `requester` — was *designed* (#2187) to be recorded in the WAL as `task_subscribed` and `task_rebound` entries, so that (being part of the runtime substrate) it would be rewound to the target seq along with conversation state. That design was never wired to a writer, and #3436 removed the `task_subscribed`/`task_rebound` kinds from the WAL vocabulary entirely (dead declaration, no producer — same defect class as #3357). No binding is recorded today, and there is nothing for rewind to restore here; a future task↔session-binding-rewind feature would need a real writer AND a route_key for `AgentSnapshot.apply_events` to dispatch on, not just a re-added vocabulary entry.

**What does NOT get rewound (external master):** The task backend (sqlite by default) is the external master of task state — `status`, DAG, and content. External systems cannot be wound back, so Reyn does not attempt to. On recovery and after rewind, Reyn re-reads the backend's current task-state to re-adapt: the binding is at the past point; the task-state is the live present. This is what keeps the external world clean — Reyn's internal trajectory can branch and replay, but a Jira issue's state or a completed task's result is never silently reverted.

### Chat-history visibility filter (`wal_seq` anchor)

Global rewind moves the runtime substrate's active cut, but the **chat message history** (`ChatMessage` list) is a separate structure that must independently honor that cut when it is rendered back to the LLM — otherwise runtime state rewinds while the LLM still sees conversation turns from an abandoned future.

**Anchoring (`#2360`).** Each `user`/`agent`-role turn is stamped with `msg.meta["wal_seq"] = state_log.current_seq` at append time (system/summary-role turns keep `seq=0` and are not part of the ordering the slicer uses). This ties the turn to the WAL seq active when it was appended, so `is_active_seq` can later classify it as active or abandoned exactly like every other WAL-derived structure. The stamp is guarded on `state_log` presence (no WAL → no rewind → always visible) and is skipped if the turn is already anchored (a re-append keeps its original anchor). `wal_seq` lives only in `meta` and is excluded from the wire dicts `build_history` emits, so it never reaches the LLM directly — it is bookkeeping for the filter, not model input.

**Tool-cycle-aware filtering (`#2360`, tool-cycle-aware follow-up).** A GLOBAL rewind cut can land at a WAL seq that is a clean turn boundary for the rewound session but falls MID-tool-cycle for another session's conversation — the assistant `tool_calls` turn's anchor may be ≤ the cut while its tool-result turns' anchors are > the cut, or the reverse. A **flat per-turn filter** (apply `is_active` independently to each turn) would then emit a dangling `tool_calls`-without-results or a tool-result-without-`tool_calls` — providers reject that shape with a `BadRequest` (the `#2290`/`#2289` adjacency class). The filter therefore treats a **tool cycle** — one assistant `tool_calls` turn plus its immediately-following tool-result turns — as a single atomic visible unit, governed by the assistant turn's own anchor: the whole cycle is visible iff that governing anchor is active. This makes the emitted history well-formed by construction, with no post-hoc repair step.

### WAL vs audit-event separation

The WAL (`StateLog`, `.reyn/state/wal.jsonl`) and the P6 audit event log (`EventStore`, `.reyn/events/<run_id>.jsonl`) are **separate logs with different contracts**:

| | WAL (StateLog) | Audit log (EventStore) |
|--|--|--|
| Purpose | Crash recovery and time-travel reconstruct | Audit trail and replay |
| Durability | fsync per append (via `DurabilityWorker`; `step_started` blocks task loop, others FAF) | Rotation-based (not per-append fsync) |
| Lifecycle | Truncatable after snapshot | Append-only, rotation-based |
| Unification | **Prohibited** — durability-contract requirements differ | — |

Do not conflate or merge the two logs. See [Events](events.md) for details.

### Restore and schema migration

`restore_to_seq` re-runs the schema migration on the restored generation (via the shared `_migrate_columns` helper, the same one `_open` uses on first open), bringing any restored generation to the current schema. This means a generation snapshotted before an additive column was introduced is automatically upgraded on restore — there is no "old schema is frozen in the snapshot" risk. The migration is idempotent: the `PRAGMA table_info` guard skips any column that already exists, so re-running it over a current-schema generation is a safe no-op. Robust to additive schema evolution by construction.

**Cross-version restore is supported by construction.** A snapshot written under an older column set (a generation captured before an additive column was introduced) restores cleanly — `restore_to_seq` re-runs the idempotent column migration after the file-swap reopen, bringing the restored database to the current schema automatically. There is no restriction to same-version generations: the idempotent migration is the guarantee.

---

## Cost

Time-travel is on by default and carries a constant per-boundary cost. The two contributors:

1. **WAL fsync-per-append** — synchronous durability so a crash loses nothing (see the WAL table above).
2. **AgentSnapshot generation** — the runtime snapshot written at each checkpoint boundary.

Both are the price of crash-recovery and time-travel fidelity; neither is optional.

---

## Relationship to crash recovery

| | Crash recovery | Time-travel |
|--|--|--|
| Trigger | Automatic on unexpected failure | User-initiated (`/rewind`) |
| Direction | Restore to last durable generation (no forward-replay) | Backward to a past checkpoint |
| Workspace | Not rewound | Not rewound (`.reyn/` state only — git-free) |
| Branching | None | Fork / branch tree |
| Mechanism | Generation-based restore — config / snapshot / identity / pipeline generations, seq-keyed, latest-active-branch-wins, no forward-replay (`config_generations.py`, `snapshot_generations.py`, `agent_identity_generations.py`, `pipeline_recovery.py`) | PITR snapshot + WAL-diff reconstruct, git-free |
| Design | [ADR-0002](../../deep-dives/decisions/0002-forward-replay-resume.md) — superseded; the phase-level forward-replay it adopted was removed with the phase-graph engine (#2434/#2439), replaced by the generation model above | [ADR-0038 draft](https://github.com/tya5/reyn/pull/1536) |

## See also

- [Sessions](../multi-agent/sessions.md) — the Agent / Session model that a rewind cuts across

- [Events and the WAL](events.md)
- [How to use rewind](../../guide/for-users/time-travel.md)
