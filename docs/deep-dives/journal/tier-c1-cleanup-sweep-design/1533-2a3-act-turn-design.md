# #1533 2a-3 — act-turn runtime-only rewind (design)

**Status**: IMPLEMENTED on `feat/1533-2a3-act-turn` (off merged main, 2a-2 in #1559).
Author: dogfood-coder. Final 2a substrate stage. Lead scope (D6 Phase-2):
act-turn-granularity runtime-only rewind + UX framing + coherent-workspace deferral sub-issue.

> Landed: `SkillResumeCoordinator.plan_for_act_turn_rewind(*, snapshot, wal_events, target_seq)` — truncates `committed_steps`/`ambiguous_steps` at `target_seq` (reuse `SkillResumeAnalyzer`, runtime-only by construction). Tests: `tests/test_act_turn_rewind_2a3.py`. Coherent-workspace deferral tracked as #1560.

## What act-turn rewind is (and why it's a distinct path)

Phase-1 `checkout(seq)` rewinds at **boundary** granularity (turn / phase /
plan-step) — it reconstructs the *agent* snapshot (inbox replay) + workspace.
But a mid-turn **step** within a skill run is not agent-snapshot state; it's
skill-run execution state (current phase + which committed steps within it). The
agent reconstruct can't reach it.

ADR-0038 D6: act-turn is **not a durable checkpoint** (ADR-0002 rejected mid-act
state on volume grounds) but **is reachable** via `snapshot(step-start) +
CommittedStep memo` **0-token Ghost-Replay**. That machinery already exists for
crash-resume:

- `SkillResumeAnalyzer.analyze(snapshot, wal_events)` → `ResumePlan` with
  `committed_steps` (each `CommittedStep` carries its `seq` + recorded `result`).
- `OSRuntime.run(resume_plan=plan)` → `dispatch_tool` consults
  `resume_plan.committed_steps` via `_lookup_memoized_step` (op_invocation_id +
  args_hash): a match returns the recorded result with **no re-execution**
  (0-token), so steps replay as ghosts; unmemoized steps execute normally.

## The insight: act-turn rewind = truncate the memo at the target seq

To rewind a run to act-turn boundary **K** (a `CommittedStep.seq`): build the
ResumePlan, then **keep only `committed_steps` with `seq <= K`**. On relaunch,
steps ≤ K Ghost-Replay (0-token), steps > K fall out of the memo and re-execute.
That *is* the rewind — no new replay engine, pure reuse of the analyzer + the
existing dispatch memo. Runtime-only by construction (it touches only the memo /
skill-run state, never the workspace).

## Proposed surface

`SkillResumeCoordinator.plan_for_act_turn_rewind(*, snapshot, wal_events,
target_seq) -> ResumePlan`:
- analyze → full plan, then `replace(plan, committed_steps=[c for c in
  committed_steps if c.seq <= target_seq])`.
- ambiguous_steps similarly bounded (`started_seq <= target_seq`) — a start after
  K is not part of the rewound-to state.
- returned plan feeds the existing `OSRuntime.run(resume_plan=...)` launch path
  (no new runtime wiring; the relaunch seam is unchanged).

(API home = the coordinator, beside `decide_for_plan` — it's the resume-plan
shaping layer. Open to analyzer instead if lead prefers; flagged below.)

## Runtime-only — explicit UX framing / documented limitation

Act-turn rewind restores **runtime/skill-execution state only**. The workspace is
**NOT** rewound to mid-act-turn coherence: file ops performed *within* the
act-turn are not content-versioned per-step (the shadow-git blob store D9 captures
at boundary generations, not per-op). So after an act-turn rewind the workspace
reflects the last **boundary** generation ≤ the enclosing turn, not the precise
mid-step file state. This is surfaced to the user as a documented limitation
(act-turn rewind = "re-run from step K with memoized history", not "the repo as it
was mid-step K").

## Deferral → tracked sub-issue (like #1557)

**Coherent act-turn workspace** (file content rewound to mid-step precision)
requires a **per-file-op content log** (every file mutation captured as a blob,
not just boundary snapshots). That's a substantial substrate addition, explicitly
deferred by lead. Tracked as a sub-issue of #1533 (NOT `wait_owner_iv` — it's a
tracking issue, not a user-intervention request).

## Test plan (TDD, real-instance)

- truncate-at-K: a run with committed steps at seqs {.., K, ..} → plan' has
  exactly `committed_steps` with seq ≤ K; later steps dropped (→ they re-execute).
- ambiguous bounded by target_seq.
- target at/after the last step = full plan (no-op rewind); target before the
  first step = empty memo (full re-execution from phase start).
- real `SkillResumeAnalyzer` + real WAL events (no mocks); first-line `"""Tier 2: ..."""`.
