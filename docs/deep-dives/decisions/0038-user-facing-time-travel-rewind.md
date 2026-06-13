# ADR-0038: User-facing time-travel — global consistent-cut rewind + PITR snapshot generations

**Status**: **DRAFT / Proposed** (design-first — pending lead review + owner
final confirm; NO impl until gate passes). Authored 2026-06-13 for issue #1533.
**Track**: Core state-model — successor seam to ADR-0001 (WAL+snapshot),
ADR-0002 (forward-replay), ADR-0023 (PlanSnapshot).
**Owner status**: design judgments co-designed + confirmed with owner (issue
#1533). This ADR records the decision + grounds it in the live code seams.

---

## Context — three layers, only the third is new

Reyn already has two of the three "time-travel" capabilities; this ADR adds the
third (user-facing rewind/resume). Keeping them distinct is load-bearing:

| Layer | What | Status | Mechanism |
|---|---|---|---|
| time-travel **debugging** | read-only walk / seek / compare a past run | **done** | `ReplayEngine` (`src/reyn/replay/engine.py`) + dogfood-trace |
| **crash recovery** | re-execute from the crash point (= head) | **done** | WAL forward-replay (ADR-0002) |
| user-facing **rewind/resume** | jump back to an *arbitrary* checkpoint and continue / branch | **NEW (#1533)** | this ADR |

The crash-recovery path already reconstructs "head" from `snapshot + WAL delta`.
User-facing rewind generalises that to "reconstruct arbitrary seq N", which is a
**point-in-time-recovery (PITR)** problem, plus an append-only **branch** model
so undo never destroys audit history.

---

## The live state-model (flow-trace) — what we extend

Grounded in the current implementation (the seams a rewind must hook):

### WAL — `src/reyn/events/state_log.py` (`StateLog`)
- Global, single-sequence, append-only WAL. `append(kind, **fields) -> seq`
  assigns a **monotonic global `seq`** and `fsync`s per append (durability).
- `iter_from(min_seq)` yields entries `seq >= min_seq` in file order — the
  **forward-replay read seam**.
- `truncate_below(min_keep_seq)` atomically rewrites the WAL keeping
  `seq >= min_keep_seq` (tmp-write → fsync → rename). Caller computes
  `min_keep_seq = min(all agent applied_seq, all active skill
  last_phase_applied_seq) + 1` — the **retention seam**.
- `_scan_max_seq` rebuilds the counter on load; `truncate_below` deliberately
  never drops the highest present seq (so the counter never reissues a used seq).

### Snapshot — `src/reyn/events/agent_snapshot.py` + `chat/services/snapshot_journal.py`
- `AgentSnapshot`: per-agent materialized state — `applied_seq` (highest WAL seq
  baked in), `inbox`, `pending_chains`, `active_skill_run_ids`,
  `outstanding_interventions`, `buffered_intervention_answers`, `active_plan_ids`.
  `load`/`save` are atomic (tmp → fsync → rename), with a `schema_version` refuse.
- `SnapshotJournal` holds the **single current** in-memory snapshot and, on each
  state change, appends to the WAL **and** advances `snapshot.applied_seq`.
- **Key gap for PITR**: today there is exactly **one** snapshot per agent (the
  latest). There are no *generations*. You can reconstruct `head`
  (`snapshot + iter_from(applied_seq+1)`) but not an arbitrary past seq N.

### Resume / forward-replay — `src/reyn/skill/skill_resume_analyzer.py`
- `SkillResumeAnalyzer.analyze()` reads WAL events past `snapshot.applied_seq`
  and produces a `ResumePlan` of `CommittedStep` (memoizable — **0-token Ghost
  Replay**) and `AmbiguousStep` (operator decision). This is the existing
  "replay forward without re-invoking the LLM" engine — the seam Phase-2
  act-turn granularity reuses.

### Orchestration — `src/reyn/chat/registry.py` (`AgentRegistry`)
- `restore_all` rebuilds every agent from its snapshot + WAL on startup and is
  where truncation is driven. The **rewind entry point** lives here (system-wide
  reconstruct-as-of-N).

### Cancel — `#1470 cancel_inflight()` (`chat/router_loop.py`, `session.py`)
- Cooperative cancellation of an in-flight chain (background task → act-turn →
  in-flight op; subprocess via `cancel_inflight`). Rewind reuses this cascade.

---

## Decision

### D1. PITR model — snapshot **generations** + WAL delta
Reconstructing arbitrary seq N = **nearest snapshot generation (`applied_seq ≤ N`)
+ `iter_from` WAL delta replayed forward to N**. Crash-recovery (N = head) becomes
the special case. Snapshots are **full** (owner: snapshots are small → no delta
chain; reconstruction stays a simple "load + replay"). Snapshot **generations are
cut at turn / plan-step / phase boundaries** — i.e. exactly the user-facing
checkpoint granularity. `PlanSnapshot` (ADR-0023) folds in as one generation kind.

### D2. GLOBAL consistent cut (owner expectation — confirmed)
A rewind to seq N is a **global consistent cut**: every agent, every plan, the
router, and the single shared workspace move to as-of-N together. This is
**architecture-enforced**, not a feature we bolt on:
- WAL is a **global single-seq** log (ADR-0001) — one N orders everything.
- The workspace is **one shared SSoT** (P5) — one revert covers all.
- cross-agent ordering is already coupled through that single seq.
Mental model = VM snapshot / `git checkout` of the whole repo. **All in-flight
tasks are cancelled** on rewind.

### D3. rewind = compensating-forward record + active-pointer reset (append-only)
Rewind is **not** backward deletion. It appends a `rewind`/`reset` record to the
WAL that moves the **active pointer** to seq N; entries `> N` are retained as an
**inactive branch** (P6 audit + WAL stay append-only). Therefore every undo is
internally a **fork**. **Core wiring (Phase 1)**: WAL replay and snapshot
derivation must **honor the reset record** — after a rewind, reconstruction (incl.
a crash mid-rewind) yields the as-of-N state and must **not** resurrect the
"discarded future".

### D4. cancel = live in-flight chain only
Cancel targets **only chains executing forward at rewind time** (idle ⇒ no cancel,
just state restore). Cascade: background task → current act-turn → in-flight op
(LLM / tool / subprocess via `#1470 cancel_inflight`). A global rewind cancels all
in-flight.

### D5. retention = 2 windows, config-driven
Two retention windows: **WAL (fine)** + **snapshot generations (coarse)**.
Default = current behaviour (live floor = `min(applied_seq)+1`); **opt-in deeper**
for a usable rewind horizon. The knobs (truncation floor / size threshold (current
1 MB) / semantic-boundary gate) are consolidated into a single **retention
policy**. Rewind is **bounded by the retention window** — you cannot rewind into
truncated history. The resume window (WAL, truncatable) and inspect window
(EventStore rotation) stay separate; the latter is longer.

**Abandoned-branch retention** (clarified in review): after a rewind, the inactive
branch `(N+1 .. reset-1)` is **not needed for recovery** (recovery follows the
active pointer, D3), so it could be WAL-truncated immediately — a direct benefit of
WAL/audit separation. **But Phase-2 fork needs it** to reconstruct a forked branch.
Resolution: within the retention window the abandoned branch is **retained
(fork-capable)**; outside the window it is **GC'd**. Audit history of the abandoned
branch survives in the EventStore regardless (independent rotation). The blob store
(D9) GCs unreferenced workspace blobs on the same window boundary.

### D6. granularity — phased
- User-facing = **chat-turn / plan-step / phase** boundaries (snapshot generations).
- **act-turn** is not a durable checkpoint (ADR-0002 rejected mid-act-turn state on
  volume grounds) but **is reachable** via `snapshot(step-start) + CommittedStep
  memo` 0-token Ghost Replay.
- **Phase 1 = boundaries only.** **Phase 2 = act-turn via memo.**

### D7. surface — TUI `/rewind` (+ Esc-Esc) primary
Unified snapshot-generation timeline (turn / phase) for users; WAL step layer for
debug. Web (Chainlit) follows. dogfood-trace CLI stays for dev.

### D8. undo / fork — git-like, single live branch (owner-confirmed)
The append-only inactive-branch model (D3) **is a branch tree** — every branch
physically retained. Two operations sit on it:
- **undo** = move HEAD (the active pointer) back to seq N — a reset. **(Phase 1)**
- **fork** = branch at N + checkout — continue on a new branch from a past point.
  **(Phase 2 UX; foundation built in Phase 1)**

**Exactly one branch is live (checked out) at a time**, enforced by the single
shared workspace + global rewind (D2). The reconstruct/reset foundation is
**fork-capable from Phase 1** (Phase 1 must not design fork *out*); only the fork
surface/UX lands in Phase 2. Mental model = git: a tree of branches, one checked
out; undo and fork coexist on the same substrate.

### D9. Workspace file-content rewind — content-addressed blob store (raised in review)
D2's "workspace moves to as-of-N" requires rewinding **workspace file content**,
not just runtime state. **Distinction surfaced in lead review**: `AgentSnapshot`
(D1) carries **runtime state only** (inbox / chains / skill-run ids /
interventions / plan ids) — it holds **no** workspace artifact/code blob, and there
is **no workspace versioning in `src` today**. So a global cut has two halves:
"restore agent/conversation state" (runtime AgentSnapshot) **and** "restore code"
(workspace files). The second is missing and must be added.

Owner's "snapshots are small → full snapshot" applies to the **runtime
AgentSnapshot**; **workspace files are a separate axis and can be large**, so
full-copy generations would be heavy. **Recommended (for owner confirm)**: a
**content-addressed / git-like blob store** — each generation references workspace
files by content hash, unchanged files shared (de-duplicated) across generations;
reconstruct-as-of-N restores the tree from the generation's blob manifest + WAL
file-op delta. This matches the git mental model (D8) and the shadow-git approach
Claude Code uses for code restore. **Storage strategy is an owner question**:
full-copy vs content-addressed blob store vs OS-level CoW/reflink — cost vs
simplicity. (Like Claude Code, external/irreversible side effects — sent
messages, real subprocess writes outside the workspace — are **not** rewound; only
workspace files + runtime state.)

### Reset-record + active-pointer semantics (incl. nested rewind) — proposed for (b)
**Reset-record** (WAL entry, fsync'd like any append):
`{kind: "rewind", seq: R, target_n: N, supersedes: <prior active-pointer seq | null>}`.
- **Active pointer** = the reset-record with the **highest `seq`** (latest wins);
  if none, the active pointer is `head`.
- **Active path** = the WAL minus the **abandoned segments** `[target_n, R)` defined
  by the reset-record chain, resolved **latest-first** (a later rewind can abandon
  an earlier rewind's branch). `reconstruct(N)` = nearest snapshot generation `≤ N`
  on the active path + replay of active entries `≤ N`.
- **Nested rewind** (rewind of a rewind) is just another reset-record at a higher
  `seq`; it supersedes the prior active pointer and may abandon an earlier rewind's
  branch. No special case — the "latest reset-record wins + abandoned-segment
  union" rule composes.
- **Crash-mid-rewind idempotence (keystone)**: because the reset-record is fsync'd
  **before** reconstruction begins, a restart re-derives the same active pointer and
  thus the same as-of-N state — the discarded future is never resurrected. The
  correctness test must cover **nested rewind + crash mid-rewind**.
- *Alternative considered*: tag every WAL entry with a `branch_id` and resolve by
  branch lineage (direct git model). Rejected for Phase 1 as a heavier per-entry
  schema change; the reset-record-on-single-WAL model fits the global-single-seq
  substrate (D2) with no per-entry change. Revisit if nested-rewind path resolution
  proves expensive at scale.

---

## Rejected alternatives

- **Per-agent scoped rewind** — rewinding one agent independently. Rejected:
  requires splitting the workspace + WAL per agent, which collides head-on with
  the single-SSoT / global-single-seq architecture (D2). The global cut is both
  simpler and what the architecture already enforces. (For the common
  single-agent / single-chat case the global cut *feels* local anyway.)
- **Backward deletion of post-N entries** — physically truncating the future on
  undo. Rejected: violates WAL/P6 append-only + destroys audit history. Use the
  compensating-record + inactive-branch model (D3).
- **Delta-chain snapshots** — incremental snapshots to save space. Rejected
  (owner): snapshots are small; full snapshots keep reconstruction a trivial
  "load + replay" and avoid chain-rebuild complexity.
- **Concurrent live forks** — running two branches live at the same time.
  Rejected (owner, explicit): a second live branch needs a second workspace,
  which destroys the single-SSoT invariant (P5 / D2). The branch tree is fully
  retained, but **only one branch is checked out at a time** — switching is a
  checkout, not a parallel run. (Distinct from D8's undo/fork, which both live on
  one checked-out branch.)
- **Treating the EventStore (P6 audit log) as the rewind source** — conflating
  audit with recovery. Rejected: recovery derives from the **WAL/StateLog**
  (fsync-per-append durability), audit from the **EventStore** (rotation /
  observability). They are separate logs (see `project_wal_vs_audit_event_separation`).

---

## Implementation plan (phased; impl gated on review + owner confirm)

### Phase 1 — boundary-granularity global rewind + PITR + reset-record
1. **Snapshot generations (runtime)**: extend `SnapshotJournal` / `AgentSnapshot`
   persistence to retain *generations* keyed by boundary seq (turn / plan-step /
   phase), not just the latest. Fold `PlanSnapshot` in as a generation kind.
   (Atomic-write pattern reused.)
1b. **Workspace blob store (D9)**: content-addressed store for workspace file
   content; each generation carries a blob manifest (file → content hash); blobs
   shared across generations. `reconstruct(N)` restores the tree from the
   generation manifest + WAL file-op delta. (Owner picks storage strategy; the seam
   — capture workspace tree at each generation boundary, restore on rewind — is the
   same regardless.)
2. **PITR reconstruct(N)**: add a reconstruct-as-of-N path = nearest generation
   `≤ N` + `StateLog.iter_from` replayed to N. Crash-recovery `restore_all`
   becomes `reconstruct(head)`.
3. **Reset-record honor (core wiring)**: define a `rewind`/`reset` WAL record;
   make `iter_from`-driven replay + snapshot derivation **honor** the active
   pointer (entries `> N` from before the latest reset are inactive). This is the
   correctness keystone — verify with a "rewind then crash mid-rewind" replay test.
4. **Global cut + cancel**: rewind entry point in `AgentRegistry` that (a) cancels
   all in-flight via `cancel_inflight` cascade, (b) appends the reset record,
   (c) reconstructs every agent + workspace as-of-N.
5. **Retention policy (2-window, config)**: consolidate truncation floor / size
   threshold / semantic-boundary gate; add the coarse snapshot-generation window;
   default current + opt-in deeper. Rewind bounded by window.
6. **TUI `/rewind` (+ Esc-Esc)**: timeline of snapshot generations; select → global
   rewind. Quiescent-only or system-wide-rollback **warn** when multi-agent
   concurrency is live.
7. **Doc-fix**: stale `planner.py:32` "in-memory, no workspace persistence MVP"
   comment (ADR-0023 already persists).

### Phase 2 — act-turn granularity + branch/edit + web
8. **act-turn checkpoints** via `snapshot(step-start) + CommittedStep` 0-token
   Ghost Replay (reuse `SkillResumeAnalyzer`).
9. **edit / fork** UX on the inactive-branch model.
10. **web (Chainlit) surface** + **multi-plan rewind-time cancel** hardening.

---

## Consequences

- **Positive**: user-facing rewind/resume; crash-recovery unified as PITR N=head;
  audit history preserved (append-only branches); architecture-enforced global
  consistency (no per-agent split needed).
- **Cost**: snapshot generations increase storage in the coarse window (bounded by
  retention config); the reset-record-honor wiring touches the replay/derivation
  hot path (must be correctness-tested, incl. crash-mid-rewind).
- **Risk surfaced for review / owner questions**: (a) generation cut frequency vs
  storage **+ workspace blob-store strategy (D9): full-copy vs content-addressed vs
  OS-level CoW/reflink**; (b) reset-record semantics under nested rewinds — *proposed
  above (latest-reset-wins + abandoned-segment union, crash-idempotent); needs
  lead sign-off + nested+crash test*; (c) multi-agent concurrent-rewind UX (warn vs
  quiescent-gate) — flagged for owner.

---

## References
- ADR-0001 (WAL + snapshot) / ADR-0002 (forward-replay) / ADR-0023 (PlanSnapshot)
- #1470 (`cancel_inflight`) / `ReplayEngine` (`src/reyn/replay/engine.py`)
- Live seams: `events/state_log.py`, `events/agent_snapshot.py`,
  `chat/services/snapshot_journal.py`, `skill/skill_resume_analyzer.py`,
  `chat/registry.py`
- Competitor grounding: LangGraph (`get_state_history` + `update_state` +
  `checkpoint_id` fork, super-step granularity); Agent VCR (Record/Rewind/Edit/
  Resume, frame granularity, memo Ghost Replay); Claude Code (`/rewind`, Esc-Esc,
  message granularity, external/bash not reverted)
