# Unified durability IO worker — design (WIP)

> Status: **design settled (2026-06-28), implementation staged.** Tracking:
> [#1765](https://github.com/tya5/reyn/issues/1765) (fsync responsiveness) +
> [#1763](https://github.com/tya5/reyn/issues/1763) (workspace↔WAL durability
> asymmetry). Owner-driven design; this doc records the rationale and — per
> owner request — **especially the constraints the lead-coder initially missed
> and the owner caught**, so they are not re-discovered the hard way.

## Problem

The WAL `StateLog.append` (`state_log.py`) runs `os.fsync` **synchronously on
the event loop** — deliberately (the "append is event-loop-atomic, no
intra-append suspension" invariant). `append` is called at high frequency
(dispatch / op / postprocessor / per-LLM-call / registry / snapshot / task), so
each fsync blocks the loop → TUI repaint / responsiveness degradation (#1765).

A separate, related gap (#1763): the **workspace** (the agent's working files,
through which inter-phase data flows under P5) is **not fsync'd and not ordered
with the WAL**. On power-loss, the WAL+snapshot are durable but workspace bytes
can be lost → recovery resumes *past* a workspace write whose file never landed
→ runtime⇄workspace divergence.

## Prior art that constrains the solution — #1751 (READ before designing)

A naive fix (`os.fsync` → `await asyncio.to_thread(os.fsync)`, #1753) was
**reverted** (`e5cc9b99`). The diff held `self._lock` across the await and
claimed "no other append can interleave = safe". The revert proved that
**insufficient**. The real failure mode is **append-vs-READ, not
append-vs-append**:

- `iter_from` (the WAL reader) is a **lockless synchronous generator**.
- During the off-loop `await fsync`, a concurrent `iter_from` can read a
  **written-but-not-yet-fsync'd** entry from the page cache → on crash that seq
  is lost, yet a reader already observed it = a **non-durable read**.

**Lesson: holding the append lock across the await does not protect the read
path.** Any off-load design must structurally guarantee readers never observe
un-durable data.

## Converged design — a single serial durability worker

All durable substrates — **WAL appends + snapshots/truncates + workspace
writes** — funnel through **one serial async worker** that owns the files. This
is the single serialization point.

- **Off-loop fsync**: the worker runs `await asyncio.to_thread(os.fsync, fd)`.
  Python has no portable true-async fsync; `to_thread` (a shared thread pool)
  is the awaitable form (io_uring `IORING_OP_FSYNC` is a Linux-only, non-stdlib
  upper option, noted but not chosen).
- **Serialization solves #1751 by construction**: because the worker is the
  *sole* writer and the append coroutine does **not hold the file across a
  yield** (it only enqueues + awaits an ack), the "#1751 append holds the file
  mid-commit during yield" surface does not exist. The owner's single-worker
  proposal *is* the structural fix for #1751.
- **Durable watermark `_durable_seq`** (last fsync'd seq, bumped *after* each
  fsync). `iter_from` returns only `seq ≤ _durable_seq`, so a live reader never
  sees the un-fsync'd tail — closing the #1751 read surface **structurally**
  (not via a "cold reads are quiescent" operational assumption). `iter_from`
  stays sync/lockless → the many recovery/rewind call-sites are unchanged
  (minimal blast radius).

## Implementation architecture

A single **substrate-agnostic `DurabilityWorker`** (`core/events/`) serializes
all durability ops. It does **not** know about WAL / snapshot / workspace
(P7-clean): each submitted task is a substrate-provided `async () -> None` that
does the write + `await asyncio.to_thread(os.fsync, fd)`. Submissions are
processed **strictly serially and in order** — so **submit order = durability
order**, the single point that gives cross-substrate ordering (and hence the
write-ahead guarantee, by submitting the depended-on write before the depending
one).

The **`submit(do_write)` interface is the stable seam**; the internal mechanism
evolves under it without changing callers (non-throwaway routing):

- **Step 1 (blocking): a fair `asyncio.Lock`** gives serial-FIFO + off-loop
  fsync + durable-before-return with **no background task** (chosen over a
  background-task+queue, which leaked "Task destroyed but pending" in
  per-test loops). `submit` acquires the lock, runs the write+fsync, returns —
  the caller `await`s durability.
- **Step 2 (non-blocking): swap the internal to a queue + drainer** so the
  drainer can group-commit (drain N → one fsync → ack all) and `submit` can
  return without awaiting (only the barrier awaits). The seam (`submit` →
  ack-future) supports both internals, so this is a contained internal swap.
- Substrate-specific state (e.g. the WAL's `_durable_seq` watermark) lives in
  the substrate's write callable, not the worker. The **watermark — not the
  lock — protects readers** (`iter_from ≤ _durable_seq`), so the #1751
  read-closure holds regardless of the internal mechanism.
- `aclose` drains pending work (shutdown flush).

Step-1a routing: `state_log.append` and `truncate_below` and `snapshot save` each
`submit` their write+fsync. Per mutation, `WAL submit → ack → snapshot submit →
ack` (blocking) makes the WAL durable before the snapshot, so the snapshot's
`applied_seq` is durable strictly after the WAL seq it names. Step 2 keeps the
same ordering via queue order without the awaits.

## Staging (owner-decided, recovery-safe at every step)

- **Step 1 — unified worker + write-ahead ordering, still BLOCKING.**
  `append` enqueues `(entry, ack_future)` and `await`s the ack; the worker
  fsyncs then resolves it. **Completeness invariant** (durable-before-return,
  no window) — identical durability to today's sync fsync. Fixes the loop-block
  (responsiveness) and establishes the WAL/snapshot/workspace ordering. The
  turn still waits per append.
- **Step 2 — NON-BLOCKING writes.** `append` enqueues and proceeds (async
  durability); a **durability barrier** awaits durability only before an
  *external effect* (message sent, user-visible file, irreversible op). This is
  the real benefit (turn-fast).
- **Step 3 — group-commit + write-through read-cache + reordering.** Throughput.

The Step-1 worker is the foundation Steps 2–3 extend (batching, optional-await,
cache) — **no throwaway**.

## Constraints the lead initially MISSED — the owner caught these

Recorded explicitly per owner request, because each is a real correctness gate.

1. **Blocking (Step 1) is not the goal; non-blocking is.** Off-loop *blocking*
   writes make the *system* responsive (the loop is free during fsync) but the
   *writing turn still waits* per append. The real win is non-blocking writes
   (the turn does not wait). The lead's first framing ("Step 1 = A, simple,
   loop-free, done") under-valued this.

2. **Non-blocking writes REQUIRE snapshot↔WAL serialization.** A non-blocking
   WAL write returns before fsync (a durability window). If a snapshot is taken
   assuming "WAL durable up to seq S" while S's fsync is still pending, a crash
   leaves the snapshot inconsistent with the WAL → broken recovery. **The
   snapshot must wait for the WAL to be durable up to its seq** — i.e. snapshot
   and WAL must go through the same serial worker.

3. **The same applies to the WORKSPACE (this is #1763).** Verified: the
   snapshot holds *no* workspace content, and `workspace_updated` has *no*
   recovery replay handler — so the workspace is **independent durable state**
   that recovery relies on (via P5 inter-phase data flow), **not** re-derived
   from the WAL. Therefore non-blocking writes need **write-ahead ordering**:
   the workspace write (and any depended-on WAL entry) must be fsync-durable
   *before* the WAL event that depends on it, so recovery never resumes past a
   non-durable workspace state. WAL-only (or even WAL+snapshot-only) is
   insufficient once writes are non-blocking.

   **The dependency, with primary evidence (e2e):** the WAL `step_completed`
   (seq S) is the recovery pointer; the op-content-log maps seq S → `tree_sha`.
   So the **workspace tree is the depended-on content and `step_completed` is
   the depending pointer** — the tree must be durable first. Today the workspace
   capture is a **POST-append observer** (`registry.py` —
   `register_post_append(_on_wal_append_capture)`): the tree is captured *after*
   `step_completed` is already durable — the **dangerous direction** (a durable
   pointer to a not-yet-durable tree; a crash in between leaves a dangling
   pointer). The worker reverses this to **capture-before-append**: ① workspace
   tree captured + durable → ② WAL `step_completed` durable. Snapshots follow
   the same rule (content durable → the WAL records it ready).

   **Step-1 scope consequence:** the workspace write-ahead reversal must land in
   the **blocking foundation (Step 1)**, *before* non-blocking writes (Step 2).
   Deferring it would let a Step-2 non-blocking WAL pointer (an un-durable
   window) ride on the still-unfixed #1763 → un-durable pointer → missing tree →
   *worse* than today. (Implementation may split Step 1 into 1a = WAL+snapshot
   worker, 1b = workspace + write-ahead, both landing before Step 2.)

4. **The watermark must be initialized from disk at recovery.** `iter_from` is
   *the* WAL reader used by recovery / replay / rewind / snapshot
   reconstruction (10+ call-sites). If `_durable_seq` defaulted to 0 at a fresh
   (post-crash) start, `iter_from` would return nothing → **recovery replays
   nothing → broken**. The worker must init `_durable_seq` from the on-disk max
   seq at startup (e2e: `_scan_max_seq()` in `__init__`, demonstrated). The
   watermark guard excludes only the *live in-flight* tail; at restart there is
   no in-flight tail → all durable entries are returned.

5. **The snapshot's watermark is set at worker-PROCESSING time, not at
   request-issue time.** When a snapshot request is *issued*, the writes ahead
   of it in the queue are not yet fsync'd, so the as-of seq `W` is undetermined.
   The worker, processing the snapshot in FIFO order (after those writes are
   durable), determines `W = _durable_seq` and **embeds it into the snapshot**
   (the existing `applied_seq`). Recovery then loads "snapshot as-of W" + replays
   WAL from `W+1`.

6. **Snapshot CONTENT-vs-watermark consistency under non-blocking (Step 2).**
   In Step 1 (blocking) in-memory state and durable WAL are lock-step, so the
   snapshot content captured at processing time *is* as-of W. Under non-blocking
   writes (Step 2) the in-memory state can run *ahead* of W (optimistic updates
   of enqueued-but-not-durable writes); the snapshot **content** must then be
   captured as-of W — drain the queue to W before snapshotting, or version /
   copy-on-write the state at W. Embedding W is necessary but not sufficient.

7. **Hot-reload interaction with the workspace worker.** Hot-reload (the
   `HotReloader`; #2073 LLM-op hook-write self-reload) reads/writes
   hooks/config/skills files. If the worker mediates those files: (a) self-reload
   *writes* must route through the worker (durability/ordering), (b) hot-reload
   *reads* must observe the durable state (not a partial / un-fsync'd write),
   (c) change-detection must fire *after* the durable write (no mid-write
   misfire). Verify this path in the plan-first.

## Recovery safety (e2e, primary-evidence)

For the Step-1 blocking foundation, recovery is **not** worsened:
- watermark `_durable_seq` init'd from disk → full replay at restart (demonstrated);
- `truncate_below` routed through the worker (serialized by enqueue order; the
  earlier blocking-WAL-only prototype used the WAL's own `self._lock` — the
  worker subsumes that);
- completeness invariant → WAL durability identical to today → snapshot/workspace
  relative ordering unchanged (the #1763 pre-existing asymmetry is untouched, not
  worsened — it is fixed when the workspace joins the worker).

Every step's `falsify` set must include **crash → restore → replay completes**.

## Assumption the `_inflight_seq` read-closure relies on

The off-loop fsync opens a window where an entry is written-but-not-yet-fsync'd.
`iter_from` excludes the **single in-flight seq** (`_inflight_seq`, per-instance)
rather than capping at a durable ceiling — so a *non-writing* instance of a
process-shared WAL still sees every durable entry. This is sufficient **because
the access model is single-writer + quiescent-reads** (verified, Step 1a-i):
the same process has one `StateLog` instance (the registry's); all `iter_from`
callers are recovery / rewind / resume (quiescent, not concurrent with an active
append); cross-process concurrent *writers* are pre-existing-unsupported (seq
collision), and a reader (recovery) reads a *dead* writer's static WAL.

**Future consideration (out of current scope):** a live, separate-process WAL
*monitor* that reads while the writer is active would not be protected by the
per-instance `_inflight_seq` — it would need a **shared durable marker** (e.g. a
"durable-up-to-seq" file the writer fsyncs) so the reader can see the same
boundary. (The pre-change sync-fsync path had its own best-effort torn-line skip
for cross-process reads.) Not needed today; record so it is not re-discovered.

## Pre-existing perf characteristic (out of #1763 scope, follow-up)

The workspace tree capture (`capture_tree` → `git write-tree`) uses a **blocking
`subprocess.run`** (`workspace_version_store.py`), so it briefly blocks the event
loop today — a **pre-existing** characteristic, *not* introduced by the durability
worker. #1763 scope is ordering + git-object fsync only; it does not make this
worse and does not fix it. Off-loading the git subprocess (`to_thread`) is a
separate perf follow-up, flagged here so it is not silently folded into the
durability work. (The write-ahead ordering relies on `core.fsync` making the tree
durable when `capture_tree` *returns* + the await-sequence, not on it being
off-loop.)

## Future optimization (recorded, not initial scope)

**Independent-request reordering**: requests with no causal/ordering dependency
may be reordered without breaking recovery / time-travel, coalescing same-fd
writes to maximize the per-fsync batch. Requires a dependency-independence
analysis. Sits above group-commit.

## References

- #1765 (fsync responsiveness), #1763 (workspace↔WAL durability), #1751 / #1753
  (the reverted naive off-load — the read-surface lesson).
- `feedback_no_throwaway_reuse_over_industry_alignment` (A is the B/D foundation).
- `project_crash_recovery_completeness_is_differentiator` (completeness is the
  invariant; this work reduces its *cost*, it does not trade it away — except the
  bounded, barrier-guarded window introduced deliberately at Step 2).
