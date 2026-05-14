# FP-0028: Plan Progress UX — Step Description in Status Messages

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

During plan execution, status messages emitted to the user currently expose the internal `step.id` (e.g. `"plan step 2/4 done (s3)"`). This is meaningless to users who cannot map `s3` to any goal. Replacing the id with the step's human-readable description makes progress visible and gives users confidence that the plan is working on the right thing.

---

## Motivation

### Current output

```
plan step 1/4 done (s1)
plan step 2/4 done (s2)
plan step 3/4 done (s3)
```

The user has no idea what `s1`, `s2`, `s3` mean. The plan goal was "analyse the authentication flow" — whether the plan is actually making progress toward that goal is invisible.

### Expected output

```
plan step 1/4: read auth.py and identify JWT decode logic
plan step 2/4: read session.py and map session lifecycle
plan step 3/4: read middleware.py for request authentication path
```

Users can now see what was completed and calibrate whether the plan is on track.

---

## Proposed implementation

### 1. Update status emit in `execute_plan` (planner.py)

Current (line ~855):

```python
status_text = f"plan step {n_done}/{n_total} done ({step.id})"
```

Proposed:

```python
desc_preview = (step.description or step.id)[:60]
status_text = f"plan step {n_done}/{n_total}: {desc_preview}"
```

`step.description` is already populated by the planner LLM — it's the human-readable task description. The `:60` truncation keeps the message compact. Falling back to `step.id` if `description` is unexpectedly empty preserves existing behaviour.

---

## Target files

| File | Change |
|---|---|
| `src/reyn/chat/planner.py` | Status message in `execute_plan` |

---

## Dependencies

None. `PlanStep.description` is already populated.

---

## Cost estimate

SMALL — one-line change in `planner.py`.

---

## Verification

1. Run a plan with 3+ steps → status messages show truncated description, not `(sN)`.
2. If a step has an empty description (edge case) → falls back to step.id gracefully.

---

## Related

- `src/reyn/chat/planner.py` — `execute_plan`, `PlanStep.description`
- FP-0025 (`0025-planner-narration-and-sp-fixes.md`) — introduced synthesis flow this FP extends
