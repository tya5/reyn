# FP-0029: Plan Step Iteration Budget — Increase `_PLAN_STEP_MAX_ITERATIONS`

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`_PLAN_STEP_MAX_ITERATIONS = 3` in `planner.py` is too tight for realistic plan steps. A step that needs to `list_dir` → `read_file` → `write_file` (narrate) already exhausts the budget at exactly 3 turns. Any unexpected detour (a second read, a validation call) causes the step to abort silently. The router default is 5. Raising the plan-step budget to 5, with an optional `reyn.yaml` override, matches the router default and gives steps enough room to handle realistic multi-op tasks.

---

## Motivation

### Current ceiling

```python
_PLAN_STEP_MAX_ITERATIONS = 3   # list_dir + read_file + narrate = exactly 3
```

A realistic plan step:
1. `list_dir` to find the target file
2. `read_file` on the identified file
3. Needs a second `read_file` (dependency reference)  ← hits budget → **aborted**

The step silently aborts without reaching the narration turn. The `step_results` entry becomes `"(no result)"`, degrading the synthesis.

### Why 5?

- Matches `_MAX_ROUTER_ITERATIONS` (the router default).
- Provides headroom for the most common realistic patterns:
  - `list_dir` + `read_file` + `read_file` + narrate (4 ops)
  - `read_file` + `web_search` + `read_file` + narrate (4 ops)
  - Edge cases with a validation or retry (5 ops)
- Beyond 5, a step is likely runaway or confused; 5 is a safe ceiling.

---

## Proposed implementation

### 1. Raise the constant (planner.py)

```python
# Before
_PLAN_STEP_MAX_ITERATIONS = 3

# After
_PLAN_STEP_MAX_ITERATIONS = 5
```

### 2. Optional: `reyn.yaml` override

```yaml
# reyn.yaml
plan:
  step_max_iterations: 5   # default; override per-project
```

```python
# planner.py — read from config if present
_PLAN_STEP_MAX_ITERATIONS = config.plan.step_max_iterations if config else 5
```

The config override is optional (SMALL cost), but useful for projects that intentionally want tighter steps (e.g. read-only research) or looser ones (e.g. multi-file edits).

---

## Target files

| File | Change |
|---|---|
| `src/reyn/chat/planner.py` | `_PLAN_STEP_MAX_ITERATIONS` constant |
| `src/reyn/config.py` | `PlanConfig.step_max_iterations` (optional) |

---

## Dependencies

None for the constant change. Config integration depends on `PlanConfig` existing or being created in `src/reyn/config.py`.

---

## Cost estimate

SMALL — constant change is one line. Config integration is optional and adds ~5 lines.

---

## Verification

1. Construct a plan step requiring 4 ops (list_dir + 2 reads + narrate) → step completes rather than aborting silently.
2. Verify `_PLAN_STEP_MAX_ITERATIONS` = 5 in `planner.py`.
3. (Optional) Set `plan.step_max_iterations: 3` in `reyn.yaml` → budget reverts to 3.

---

## Related

- `src/reyn/chat/planner.py` — `_PLAN_STEP_MAX_ITERATIONS`, `_MAX_ROUTER_ITERATIONS`
- FP-0027 (`0027-plan-step-failure-transparency.md`) — step failures caused by hitting this budget now get forwarded to synthesis
