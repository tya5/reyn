# ADR-0025: Plan-step sub-loop LLM call memoization (R-D2 mirror)

**Status**: Accepted (2026-05-08)
**Track**: Plan-mode persistence — closes ADR-0023 §3.4 "Sub-loop work
re-paid" deferred trade-off (LLM-cost subset).

## Context

ADR-0023 §3.4 explicitly accepted that sub-loop work re-pays on
resume:

> A step that did substantial pure-LLM work without spawning a skill
> re-runs from scratch on resume. Acceptable per §3.4.

Concretely: when a plan step's sub-loop (`RouterLoop`,
`max_iterations=3`) crashes mid-flight, on resume the step is
classified as `pending` and re-executes from act-turn 1. Any LLM
calls already issued in turns 1..k re-pay; tool dispatches re-
execute; the LLM may diverge from the original chain because the
chat-history seed is identical but token sampling is non-deterministic
across providers.

R-D2 already solved this for skill-phase LLM calls via dispatcher
memoization: a `ResumePlan.committed_steps` lookup keyed on
`(op_invocation_id, args_hash)` returns the recorded `LLMCallResult`
without invoking `call_llm`. Plan-mode lacks the equivalent.

**LLM cost is the headline impact.** Tool dispatches inside a
plan step's sub-loop are cheap (file ops in milliseconds, MCP read
calls in 100 ms–1 s); LLM calls cost real money + 1–30 s wall time.
Mirroring R-D2's LLM-call memoization for plan sub-loops captures
~95 % of the value of full op-level memoization at a fraction of the
implementation surface.

## Considered alternatives

- **A. Full op-level memoization (= R-D2 + dispatcher participation).**
  Plan steps participate in `dispatch_tool`'s memo path. Highest
  fidelity; broadest implementation surface (`DispatchContext` grows
  plan-mode fields, new caller_kind, WAL event taxonomy expansion).
- **B. LLM-only memoization within sub-loop (= this ADR).** Hook
  `RouterLoop.call_llm_tools` with an optional `memo_provider`; record
  each LLM call result on the per-plan snapshot; on resume, replay
  recorded results before invoking. Tool dispatches re-execute fresh
  (= acceptable cost). **Adopted.**
- **C. Defer entirely until dogfood signal.** The original posture
  (ADR-0023 §3.4 "Acceptable per §3.4"). Now overridden by user
  judgment that LLM-cost preservation matters at production scale
  before dogfood proves it.

## Decision

Adopt **B**. Mirror R-D2's memoization shape, but at the
`call_llm_tools` boundary inside `RouterLoop` rather than at the
`dispatch_tool` boundary.

### 1. Persistence — snapshot-only, no new WAL kinds

Storage on `PlanSnapshot`:

```python
# Existing
step_results: dict[str, str] = {}        # ≤32 KB inline (ADR-0024)
step_result_refs: dict[str, str] = {}    # >32 KB spilled (ADR-0024)

# NEW
step_llm_calls: dict[str, list[dict]] = {}
# step_id → list of recorded LLM-call records, each record:
# {
#   "args_hash": str,        # 16-hex truncated SHA-256 of canonicalized inputs
#   "result_inline": dict | None,    # ≤32 KB inline JSON
#   "result_ref": str | None,         # path under step_llm_calls/<step_id>/<turn>.json (>32 KB)
#   "usage": {"prompt_tokens": int, "completion_tokens": int, ...},
# }
```

**No new WAL event kind.** Persistence is via `PlanSnapshot.save`
called after each LLM call within a step. The atomic save cost (~1 ms)
is negligible relative to the LLM call itself (1–30 s).

Rejected alternative: introducing `plan_step_llm_completed` WAL
events (= mirror skill side `step_completed` with `op_kind="llm"`).
Adds WAL volume + new event kind for marginal benefit; snapshot-only
is sufficient because the snapshot is already the cache for plan
state.

### 2. Spill (ADR-0024 mirror)

Large LLM responses (>32 KB serialised) spill to a per-plan
workspace file:

```
state/plans/<plan_id>/step_llm_calls/<step_id>/<turn_idx>.json
```

`step_llm_calls[step_id][i]["result_ref"]` holds the per-plan-dir
relative path; reads go through a new accessor (mirror
`get_step_result`).

Per-plan workspace cleanup (= `delete_plan_workspace`) already
recursively removes the entire `plans/<plan_id>/` dir, so spilled
LLM payloads are reclaimed automatically on plan completion.

### 3. Memo provider — encapsulates record + lookup

```python
# src/reyn/plan/sub_loop_memo.py
class SubLoopMemoProvider:
    """Per-step LLM memoization for plan sub-loops.

    On miss: invoke proceeds normally; record() persists result for
    future resume. On hit: invoke is skipped; recorded result
    returned. Maps args_hash → record from PlanSnapshot.step_llm_calls
    or, on resume, from PlanResumePlan.step_llm_call_log.
    """
    def get(self, args_hash: str) -> dict | None: ...
    async def record(self, args_hash: str, result: dict, usage: dict) -> None: ...
```

`get` is sync (= snapshot read), `record` is async (= snapshot save +
optional file spill).

### 4. RouterLoop integration

`RouterLoop.__init__` gains an optional `memo_provider:
SubLoopMemoProvider | None = None`. When set,
`call_llm_tools` is wrapped:

```python
# Compute args_hash from canonical inputs
args_hash = _compute_llm_args_hash(model, messages, tools, ...)

memoized = self._memo_provider.get(args_hash) if self._memo_provider else None
if memoized is not None:
    self._host.events.emit("plan_step_llm_memoized", ...)
    return _from_recorded(memoized)

# Normal call
result = await call_llm_tools(...)
if self._memo_provider is not None:
    await self._memo_provider.record(args_hash, _serialise(result), result.usage)
return result
```

`_compute_llm_args_hash` mirrors the existing R-D2 helper — same
canonicalisation rules, same `current_datetime` strip, same SHA-256
truncated to 16 hex.

### 5. Resume plumbing

`PlanResumeAnalyzer` extracts `snapshot.step_llm_calls` into a new
field on `PlanResumePlan`:

```python
@dataclass(frozen=True)
class PlanResumePlan:
    ...                        # existing fields
    step_llm_call_log: dict[str, list[dict]] = field(default_factory=dict)
    # step_id → list of {args_hash, result_inline / result_ref, usage}
```

`planner.execute_plan` constructs a `SubLoopMemoProvider` per step
on resume, seeded from `resume_plan.step_llm_call_log[step_id]`. The
provider is passed to the sub-loop's `RouterLoop`.

Per-step memo only. No cross-step memoization (= a step's LLM calls
are scoped to its own sub-loop).

### 6. Sub-loop turn ordering

`args_hash` keys cover ordering. Sub-loop turn 1 calls LLM with
prompt P1; turn 2 calls with P2 (= includes turn 1's tool result);
etc. `_compute_llm_args_hash` produces distinct hashes per turn, so
`step_llm_calls` is in effect a turn-indexed list keyed by hash.

Crash-mid-turn-3 → resume: turn 1 hash hits the recorded turn-1
result, returns memoized; turn 2 hash hits turn-2 record;
turn 3 hash misses (= the call was never recorded) → fresh execution.

### 7. Drift handling

Provider-induced drift (= `current_datetime` not stripped, model
versioning shift, etc.) surfaces as `args_hash` mismatch → fresh
execution → `record` overwrites the entry by hash. The sub-loop pays
fresh LLM cost but proceeds correctly.

`current_datetime` is already R-D2-strip-on-canonicalise; reused
verbatim.

## Consequences

### Positive

- **LLM cost preserved across crash mid-step.** A 3-turn sub-loop
  that crashed at turn 3 only pays for turn 3 on resume.
- **No new WAL volume.** Persistence rides existing snapshot save
  cadence.
- **R-D2 alignment.** Same canonicalisation + memo shape as
  skill-side LLM calls; future unification straightforward.
- **No `_compute_llm_args_hash` duplication.** Reuse the helper
  from `dispatcher.py` / `runtime.py`.

### Negative

- **Tool dispatches re-execute.** A sub-loop that called `web_fetch`
  before the crashed LLM turn re-issues the fetch on resume. World-
  purity ops (`web_search`, `web_fetch`) wanted that anyway
  (= ADR-0011); side-effect ops (= `file/write`) are uncommon in
  plan sub-loops because plan steps don't typically have write
  permissions, so this is mostly benign.
- **Snapshot save frequency increases.** Each LLM call within a
  step now saves the snapshot. On a 3-turn step, snapshot saves
  go from 1 (= step_completed) to 3-4. Atomic save is ~1 ms; total
  added I/O is negligible.
- **Snapshot grows by record metadata.** Each record holds
  `args_hash` + `usage` + (small inline OR ref). For a 3-turn step
  with all-inline ≤32 KB results, snapshot grows by ~10 KB. Spill
  threshold caps inline growth.

### Open issues / explicit non-goals

- **Tool-op memoization within sub-loops.** Out of scope; sub-loop
  tool dispatches re-execute. If dogfood reveals a hot expensive-tool
  path (e.g. heavy MCP write), revisit with full op-level memoization
  via Alternative A.
- **Cross-step memoization.** Step boundaries reset memo state. A
  multi-step plan that calls `summarise_doc` twice with the same args
  in different steps re-pays both. Acceptable: cross-step
  deduplication is a different design concern.
- **Memo for `plan` tool itself.** Plan steps' sub-loop excludes
  `plan` from its tool catalog (already enforced via
  `RouterLoop.exclude_tools={"plan"}`); no nested-plan memo to
  consider.
- **Concurrency safety.** Snapshot save is single-writer per
  PlanRegistry instance. Sub-loop awaits each LLM call serially, so
  no race within a single step. Multi-plan concurrent saves go
  through different PlanSnapshot objects (different files), no
  contention.

## Cross-references

- **R-D2** (`src/reyn/kernel/runtime.py:_call_llm_and_record`): the
  skill-side reference implementation.
- **ADR-0023 §3.4** (sub-loop work re-paid): the deferred
  trade-off this ADR closes for the LLM-call subset.
- **ADR-0024** (per-plan step result spill): the same spill pattern
  applies to LLM payloads >32 KB. Reuses
  `delete_plan_workspace` for cleanup.
- **R-D10** (`src/reyn/skill/llm_result_ref.py`): the LLM-spill
  reference; ADR-0024 + this ADR converge on the same pattern.
- **PR-memo-purity-fix M2** (world-op resume bypass): not directly
  applicable here (sub-loops don't classify ops by purity); world
  ops re-execute by virtue of "tool dispatches re-execute" in this
  ADR's scope.
