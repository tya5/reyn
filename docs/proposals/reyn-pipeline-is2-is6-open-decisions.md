# Pipeline invocation — IS-2 / IS-6 open decisions (for owner, morning review)

**Status (2026-07-04, overnight):** IS-5 (agent-callable + registry wiring, #2564)
and IS-3 (DSL parser, #2565) are merged. The next invocation slices — IS-2
(async + driver-as-session) and IS-6 (sync live-events + Ctrl-C) — are
**design-open**; this memo lays out the decisions so we can settle them quickly
in the morning rather than me guessing overnight. Nothing here is built.

Grounding: R6 in `reyn-pipeline-v0.9-design-resolutions.md` (already owner-approved
at the *shape* level: both sync and async as separate tool names; pipeline always
runs in a separate spawned driver-session for crash auto-resume). What's left is
the *interface* level.

---

## Decision 1 — the `ExecutionDriver` interface fit (the load-bearing one)

**The mismatch.** Today a session's driver is `ExecutionDriver.run_turn(user_text:
str, chain_id: str) -> None` — shaped for an LLM turn (a string utterance in,
nothing out; results drain to the outbox). A pipeline driver-session's "turn" is
different: it takes a **pipeline (name or definition) + an input object** and
produces a **`PipelineResult`**. String-in / None-out doesn't fit.

**Options:**

- **(A) Generalize the driver entry** — introduce a driver-neutral entry the two
  driver kinds each implement in their own shape. `RouterLoopDriver` keeps its
  LLM turn; a new `PipelineExecutorDriver` runs the pipeline to completion. The
  session's run path dispatches to whichever its driver exposes.
  *Pro:* honest — the two driver kinds genuinely have different entry shapes;
  no string-encoding a structured invocation. *Con:* touches the `run_turn` seam
  woven into `Session.run` / `_run_router_loop`.

- **(B) Reuse `run_turn` verbatim**, encoding the pipeline+input as the
  `user_text` string. *Rejected on sight* — encoding a structured invocation as a
  string is exactly the STRING-vs-INSTANCE drift trap we've hit before; the
  pipeline driver would have to re-parse it, and the return path still doesn't fit.

- **(C) Add a second protocol method** for pipeline entry, leaving `run_turn` for
  LLM; `Session` picks by driver type. *Pro:* smallest change to the LLM path.
  *Con:* the protocol grows a method only one implementor uses; the "1 session =
  1 driver" cleanliness blurs.

**My recommendation: (A).** It matches the architecture we already committed to
(driver-as-session; the driver *is* the run-loop). The `PipelineExecutorDriver`
is a thin wrapper over the existing, tested `PipelineExecutor.run`/`resume`
conforming to whatever the generalized entry is. This is the one decision worth
settling *before* building IS-2, because it shapes both slices.

---

## Decision 2 — build order: async (IS-2) or sync (IS-6) first?

Both are approved; the question is sequence.

- **Async first (IS-2):** fire → detached driver-session runs independently →
  result returns via an inbox event (reuses delegation's `agent_response`
  pattern). **No new infra** beyond the driver-as-session wrapper + spawn. Proves
  the whole "agent spawns a pipeline driver-session that auto-resumes on crash"
  end-to-end with the least new code.

- **Sync (IS-6):** caller stays attached and awaits; adds **live event streaming**
  (pipeline driver + agent steps → caller/TUI = the N6 observability path) **and a
  cancel checkpoint** at step boundaries (Ctrl-C). More infrastructure.

**My recommendation: IS-2 (async) first, IS-6 (sync) second.** Prove the
driver-as-session loop + auto-resume with minimal new surface, then layer live
events + Ctrl-C on top. This is the same "prove the core loop before the
observability/infra" discipline (P7) we've followed all through the pipeline arc.
It also means the load-bearing crash-auto-resume property gets proven early.

---

## Decision 3 — result-return plumbing (falls out of Decision 2, noted for completeness)

- **Async:** result delivered as an inbox event to the invoking agent, mirroring
  how a delegated `agent_response` comes back. The invoking agent's next turn sees
  it. (Needs a pipeline-result event kind + the driver-session posting it on
  completion.)
- **Sync:** result returned inline to the awaiting caller; events stream live
  during the run. (Needs the live-event path from Decision 2's IS-6.)

No open *choice* here — it's determined by sync-vs-async — but it's the concrete
plumbing each slice adds, listed so the estimate is honest.

---

## What I'd do once Decision 1 + 2 are settled

1. **IS-2**: `PipelineExecutorDriver` (thin `ExecutionDriver` conformer over
   `PipelineExecutor.run`/`resume`) + `run_pipeline_async` tool that spawns a
   driver-session (via the A2/IS-5 spawn + registry seam, `loop_driver=` injection
   from A1) + pipeline-result inbox event + crash-auto-resume test (spawn → kill →
   `restore_all` → pipeline resumes from its R4 snapshot). Recovery-feature PR →
   **truncate-falsify test required** (CLAUDE.md hard rule): the driver-session's
   resume source must survive WAL truncation below its source events.
2. **IS-6**: cancel checkpoint at step boundaries (reuse router-loop cancel
   pattern) + live event streaming to the TUI + `run_pipeline` (sync) tool.

Then: `run_pipeline_inline[_async]` (IS-4, needs the static-analysis gate) and the
non-linear primitives.

---

**Ask for the morning:** confirm Decision 1 = (A) and Decision 2 = async-first (or
steer otherwise). Those two unlock IS-2 to be dispatched the same way IS-3/IS-5
were.

---

## RESOLVED (owner GO 2026-07-05) — Decision 1 = **D** (not A), Decision 2 = async-first

A deeper pass surfaced a **fourth option that beats A**, and the owner approved it.

**Decision 1 = D — "work-order as driver birth-state; protocol UNCHANGED."**
The reframe: `run_turn(user_text, chain_id) -> None` is *already* driver-neutral —
only `RouterLoopDriver` *interprets* `user_text` as an utterance, and results
already go to the outbox, not a return value. A pipeline driver-session is
single-purpose: it is **born with its work-order** (serialized pipeline def +
input + run_id + reply-address), so the work-order is the driver's construction
state, NOT a turn payload. `run_turn` becomes a bare "run/resume" nudge. The
work-order is persisted to `.reyn/pipeline/state/<run_id>/invocation.json` at
spawn, before step 0. Uses the A1 `Session(loop_driver=...)` seam — **zero**
protocol / `Session.run` / `run_turn` change (D's edge over A, which would have
touched the LLM turn seam).

Why D > A:
- No change to the seam the whole conversation path rides on.
- Crash recovery is cleaner: the work-order is a **file** written before step 0,
  so resume = read file + rebuild driver + `executor.resume` from the R4 gen
  snapshot — no dependence on unmodelled inbox message re-delivery semantics.
  Same truncate-falsify shape as R4 (source is a file, not a WAL event).
- Free wins: N9 per-run immutable snapshot (a registry rewrite mid-run cannot
  affect a running run — its def lives in invocation.json); IS-4 inline pipelines
  recover through the *identical* mechanism (the def is persisted every run
  regardless of whether it was ever in the registry).
- The `ExecutionDriver` cancel pair (`request_cancel`/`is_cancel_requested`) is
  exactly IS-6's Ctrl-C receiver — already present, no protocol growth.

Honest wart: the pipeline driver ignores `user_text` (or asserts it matches
run_id). Small price for protocol unification.

**⚠️ Load-bearing finding from the pre-dispatch flow-trace (primary evidence):
crash-resume re-pump is NOT free.** The detached-session pump exists
(`registry.py:2601` `asyncio.create_task(session.run())`), but `restore_all`
(`registry.py:898`) only re-wakes recovery-ACTIONABLE sessions — non-empty inbox /
pending_chains / interventions, or a task-subscription via `_compute_recovery_work`
(`registry.py:1345`, task-backend-sourced). A mid-run pipeline driver-session whose
start-nudge was already consumed has an empty inbox → the #2187-5d
"RUNNING-but-empty-inbox" trap → instantiated but never re-woken → the pipeline
**silently does not resume**, defeating the whole point of driver-as-session. So
IS-2 MUST add a **pipeline-specific recovery-work source**: `restore_all` scans
`.reyn/pipeline/state/<run_id>/` for an `invocation.json` with no terminal result
and re-wakes that driver-session. It must NOT couple to the task-backend
subscription machinery — pipelines are replacing the task-system, so depending on
it is backwards (settled by strategic direction, not an open question).

**Decision 2 = async (IS-2) first, sync (IS-6) second** — confirmed. Async needs
the least new surface (fire → detached driver-session → result via inbox event,
mirroring delegation's `agent_response`) and proves crash-auto-resume earliest;
sync layers live event streaming + step-boundary Ctrl-C on top.

**Dispatch:** IS-2 sent 2026-07-05 as a two-phase task (Phase 1 = flow-trace the
two load-bearing seams — driver injection through spawn + the pipeline-recovery
re-pump — and report a concrete plan for review; Phase 2 = implement, incl. the
CLAUDE.md-mandated truncate-falsify + kill/restore/resume e2e in the same PR).
