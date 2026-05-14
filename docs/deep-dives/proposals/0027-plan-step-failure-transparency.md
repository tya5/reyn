# FP-0027: Plan Step Failure Transparency

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

When a plan step fails (exception during sub-loop execution), the failure is recorded in `PlanExecutionResult.step_failures` but is never forwarded to the router LLM that synthesises the final reply. The synthesiser sees only `step_results` and silently receives `"(no result)"` for failed dependencies — producing a confident-sounding answer with invisible gaps. This FP threads `step_failures` through to `_handle_plan_completed` so the router LLM can acknowledge what data was unavailable.

---

## Motivation

### Current silent degradation

```
step s1: reads auth.py          → result: "JWT decode at lines 78-95"
step s2: reads session.py       → FAILS (exception)
step s3: depends_on [s1, s2]    → prior_results.get("s2") == "(no result)"

_handle_plan_completed injection:
  step_results: {"s1": "JWT decode at lines 78-95", "s3": "..."}
  # s2 failure invisible — router LLM doesn't know session.py data is missing
```

The router LLM synthesises a response that appears complete, but is missing the session.py analysis entirely. The user has no way to know the answer is partial.

### Correct behavior

The router LLM should be able to say: *"I found the JWT authentication logic in auth.py, but couldn't read session.py — the session management section may be incomplete."* This requires the synthesiser to know which steps failed and why.

---

## Proposed implementation

### 1. Add `step_failures` to `_enqueue_plan_completed` (session.py)

```python
async def _enqueue_plan_completed(
    self,
    *,
    plan_id: str,
    chain_id: str,
    goal: str,
    step_results: dict[str, str],
    step_failures: dict[str, str],   # ← add
    n_steps: int,
) -> None:
    await self._put_inbox(
        "plan_completed",
        {
            "plan_id": plan_id,
            "chain_id": chain_id,
            "goal": goal,
            "step_results": step_results,
            "step_failures": step_failures,  # ← add
            "n_steps": n_steps,
        },
    )
```

### 2. Pass `result.step_failures` in `spawn_plan_task` (session.py)

```python
await self._enqueue_plan_completed(
    plan_id=plan_id,
    chain_id=parent_chain_id or chain_id,
    goal=result.plan_goal,
    step_results=result.step_results,
    step_failures=result.step_failures,  # ← add
    n_steps=result.n_steps,
)
```

`PlanExecutionResult.step_failures: dict[str, str]` already exists (maps step_id → error repr). No changes needed to `planner.py`.

### 3. Include failures in `_handle_plan_completed` injection (session.py)

```python
step_failures = payload.get("step_failures") or {}

injected_text = (
    f"[plan_completed] plan_id={plan_id}\n"
    f"goal: {goal}\n"
    f"step_results:\n{results_str}\n"
)
if step_failures:
    try:
        failures_str = json.dumps(
            {sid: err[:200] for sid, err in step_failures.items()},
            ensure_ascii=False, indent=2,
        )
    except (TypeError, ValueError):
        failures_str = repr(step_failures)
    injected_text += (
        f"\nstep_failures (steps that could not retrieve data):\n{failures_str}\n"
        "Note: synthesise from available step_results; "
        "acknowledge any gaps caused by the failed steps.\n"
    )
injected_text += "\nPlease synthesize the step results into a complete response for the user."
```

Error messages are truncated to 200 chars to avoid injecting large tracebacks into the router context.

---

## Target files

| File | Change |
|---|---|
| `src/reyn/chat/session.py` | `_enqueue_plan_completed` signature; `spawn_plan_task` call site; `_handle_plan_completed` injection |

---

## Dependencies

None. `PlanExecutionResult.step_failures` is already populated by `execute_plan`.

---

## Cost estimate

SMALL — three localised changes in `session.py`, no protocol or schema changes.

---

## Verification

1. Construct a plan where one step raises an exception → router reply acknowledges the gap ("couldn't retrieve X") rather than silently omitting it.
2. All steps succeed → `step_failures` is empty → injection is unchanged from current behaviour.
3. `plan_completion_injected` event fires in both cases.

---

## Related

- `src/reyn/chat/planner.py` — `PlanExecutionResult.step_failures` (already populated)
- `src/reyn/chat/session.py` — `_enqueue_plan_completed`, `_handle_plan_completed`, `spawn_plan_task`
- FP-0025 (`0025-planner-narration-and-sp-fixes.md`) — introduced the router narration pattern this FP extends
