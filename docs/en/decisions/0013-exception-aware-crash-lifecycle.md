# ADR-0013: Exception-aware skill completion in finally clause

**Status**: Accepted (2026-05-04)
**Track**: R-D1 (commit `c12c5b9`)

## Context

`OSRuntime.run()`'s teardown was unconditional:

```python
finally:
    if self._skill_registry:
        await self._skill_registry.complete(run_id=self.run_id)
```

`SkillRegistry.complete` deletes the per-skill snapshot and removes the
run_id from `active_skill_run_ids`. With the unconditional `complete`,
the resume machinery from D-track behaved correctly only when the
process exited via a path that bypassed `finally` (= `kill -9`).

For every other interruption path — `Ctrl-C` (`CancelledError`),
transient `RuntimeError` deep in a phase, `KeyboardInterrupt` — the
finally still ran, the snapshot was deleted, and the next process
start saw nothing to resume. The very feature D-track was built for
silently failed in the most common interruption scenarios.

The test suite obscured this for a while: `test_resume_e2e.py` raised
`RuntimeError` to simulate a crash, then the test re-saved the
snapshot manually before continuing. That manual save was masking the
production bug — production has no test fixture to re-save state.

## Considered alternatives

- **A. Always preserve snapshot, never call complete in finally.** Move
  `complete()` to the explicit completion paths inside `run()`'s body.
  Clean but spreads the call site across many `return` points; risks
  forgetting one and leaking snapshots forever.
- **B. Detect exception type via `sys.exc_info()` in finally.** Treat
  normal return (`exc_info() == (None, None, None)`) and intentional
  abort (`WorkflowAbortedError`) as completion; treat all other
  exception types as "preserve for resume".
- **C. Ask the registry which lifecycle event to fire.** Pass exception
  metadata into `complete()` and let it decide. Adds API surface; the
  decision is small enough to inline.

## Decision

**Adopt B.** New finally clause:

```python
finally:
    if self._skill_registry:
        import sys as _sys
        exc_type, _exc_val, _exc_tb = _sys.exc_info()
        if exc_type is None or issubclass(exc_type, WorkflowAbortedError):
            await self._skill_registry.complete(run_id=self.run_id)
        else:
            self.events.emit(
                "skill_run_interrupted",
                run_id=self.run_id,
                exc_type=exc_type.__name__,
                will_resume=True,
            )
```

Classification table:

| Exit path | `exc_type` | Action |
|---|---|---|
| Normal return (RunResult) | None | `complete()` |
| `WorkflowAbortedError` | matched subclass | `complete()` (skill chose to abort) |
| `BudgetExceeded` (returned via RunResult) | None | `complete()` |
| `CancelledError` | other | preserve + emit `skill_run_interrupted` |
| `KeyboardInterrupt` | other | preserve + emit `skill_run_interrupted` |
| Generic `Exception` | other | preserve + emit `skill_run_interrupted` |
| `kill -9` | finally never runs | (preserve naturally) |

`WorkflowAbortedError` is the LLM's deliberate "I'm stopping this
skill" decision, semantically equivalent to a successful run from the
state machine's perspective. Auto-resume should not retry an LLM that
already concluded "abort". Generic exceptions, by contrast, are
transient (network blip, race) and the user's auto-resume on next
startup retries them; persistent failures are the user's signal to
`/skill discard`.

## Consequences

**Positive:**

- Auto-resume ([ADR-0012](0012-auto-resume-default.md)) actually works
  for `Ctrl-C`, transient errors, and `KeyboardInterrupt`. These were
  the dominant production interruption causes; the previous behaviour
  silently lost work for all of them.
- `skill_run_interrupted` is a first-class audit event, queryable via
  `reyn events`.
- `test_resume_e2e.py`'s manual snapshot re-save was removed; the test
  now exercises the production code path.

**Negative:**

- Persistent exceptions in a phase trigger an auto-resume retry loop
  on every restart. The user's escape is `/skill discard <id>` (visible
  via `/skill list`). A future R-D item could add an automatic
  per-skill retry cap.
- Reading `sys.exc_info()` in a finally clause is slightly unusual
  Python; comments in the source explain why.

**Precluded:**

- The "always complete" simplicity. Replaced with classification
  logic, which is the right complexity for this layer.

## References

- Commit `c12c5b9` — implementation + Tier 2 tests
  (`test_runtime_crash_lifecycle.py`)
- [ADR-0012](0012-auto-resume-default.md) — the auto-resume path that
  this fix unblocks for the common case
- discussion-log Phase 12
