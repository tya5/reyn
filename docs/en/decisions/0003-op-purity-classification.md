# ADR-0003: Op purity classification for step events

**Status**: Accepted (2026-05-02)
**Track**: PR-step-events, PR-state-foundation

## Context

Resume requires step events (`step_started` / `step_completed` /
`step_failed`) in the WAL so the analyzer can tell which ops are
already done and which are ambiguous. The naive design — emit every
event for every op — produces too much WAL volume:

- Pure ops (validate, lint_plan) re-execute cheaply with the same
  result; recording them adds noise.
- Read-only ops (file/read, file/glob) have no side effect, so
  ambiguity isn't possible — `step_started` is wasted bytes.
- Side-effect ops are the ones that need ambiguity detection.

We needed to classify ops so the WAL captures only what's strictly
needed for resume correctness.

## Considered alternatives

- **A. Emit every event for every op.** Rejected: WAL volume balloons
  (50% of entries are noise) and truncation logic gets complicated.
- **B. Emit nothing; rely on artifact diffs.** Rejected: workspace
  artifacts don't capture intermediate ops (e.g. an mcp call that
  doesn't write to workspace). Loses ambiguity detection.
- **C. Per-op opt-in (`emit_events: true` in op definition).**
  Rejected: places the burden on op authors who may not understand
  the resume implications. Easy to forget.
- **D. Categorize ops by purity at framework level + selective
  emission.** Adopted. Five purity classes drive what gets emitted.

## Decision

**Adopt D: op purity classification with selective emission.**

Five purity classes:

| Purity | Examples | step_started | step_completed |
|---|---|---|---|
| `pure` | `validate`, `lint_plan`, declared-pure `python` | × | × (skip) |
| `world` (read-only, world-state-dependent) | `file/read`, `file/glob`, `mcp/call_tool` (read APIs) | × | ✓ |
| `side_effect` | `file/write`, `file/delete` | ✓ | ✓ |
| `external` | `mcp/call_tool` (write APIs), `shell`, `run_skill` | ✓ | ✓ |
| `llm` | `call_llm` (not dispatch_tool, but same lifecycle) | × | ✓ |

Rationale per class:

- **pure**: same input → same output. Re-executing on resume is fine
  and cheap; events would be noise.
- **world**: result depends on world state but no side effect. No
  ambiguity (re-execution is safe). Record the result so resume can
  use it without re-querying.
- **side_effect / external**: real side effects. Must emit
  `step_started` BEFORE invoking so the WAL records the attempt; a
  crash after this point but before completion produces an
  `AmbiguousStep` that the resume system flags for operator decision.
- **llm**: not dispatched through dispatch_tool, but conceptually
  similar — non-deterministic and expensive. Record only on
  completion (no ambiguity needed; LLM errors are transient).

`OP_KIND_REGISTRY` (`src/reyn/op_runtime/registry.py`) gains a `purity`
attribute. Dispatcher reads it to decide what to emit.

## Consequences

**Positive:**

- WAL volume reduced by ~50% (pure ops drop, world ops emit only one
  event instead of two).
- Ambiguity detection is exact: only side_effect / external ops can
  produce ambiguous states, by construction.
- Op authors who add new ops just declare purity; framework handles
  the WAL semantics.

**Negative:**

- Adds a classification axis op authors must learn. Mitigated by docs
  and concrete examples in `op_runtime/registry.py`.
- `python` op is hard to classify automatically — defaults to
  `side_effect` (safe pessimistic), with `pure: true` opt-in for
  authors who guarantee determinism. Static analysis would be ideal
  but is out of scope.

**Precluded:**

- Cannot retroactively reclassify an op without invalidating recorded
  WAL entries (their step_started semantic depends on the purity at
  emit time). In practice the change cost is small because events are
  truncated as skills complete.

## References

- Commit `5efa568` — `op_runtime/registry.py` unified op kind registry
- Plan file section "Op purity 分類による event skip"
- ADR-0001 (state model — what consumes these events)
- ADR-0002 (forward-replay resume — why ambiguity matters)
