# FP-0021: Event Log Audit Completeness — Adding run_id and actor context to missing events

**Status**: proposed
**Proposed**: 2026-05-13
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`workflow_started` correctly carries `run_id` and `skill`, but six event types emitted during
the same run do not. This means the events/*.jsonl audit log cannot be correlated per-run
without joining against `workflow_started` using timestamp proximity — fragile and incorrect
for concurrent runs. This proposal adds `run_id` and `skill` to the missing emit calls, adds
`phase` to the permission events, and introduces a `permission_granted` event (the allow path
currently emits nothing).

---

## Motivation

### The gap

`workflow_started` is the only event that always carries both `run_id` and `skill`:

```python
self.events.emit("workflow_started",
    run_id=self.run_id, skill=self.skill.name, ...)
```

The following events from the same run omit both:

| Event | Current fields | Missing |
|---|---|---|
| `workflow_finished` | `phase`, `reason`, `confidence`, `total_phase_count`, `final_output_keys` | `run_id`, `skill` |
| `llm_called` | `phase`, `model` | `run_id`, `skill` |
| `llm_response_received` | `phase`, `response_type`, `raw`, tokens, `cost_usd` | `run_id`, `skill` |
| `permission_denied` | `kind`, `path`, `reason` | `run_id`, `skill`, `phase` |
| `user_intervention_requested` | `phase`, `question`, `suggestions` | `run_id`, `skill` |
| `user_intervention_received` | `phase`, `answer` | `run_id`, `skill`, no correlation to `requested` |

Additionally: **no `permission_granted` event exists**. The allow path emits nothing, so
the audit log records denials but not approvals — an asymmetric audit trail.

### Why this matters for audit

An enterprise audit requirement of the form "show all LLM calls made by skill X in run Y"
currently requires:
1. Find the `workflow_started` event for run Y to get its timestamp range
2. Collect `llm_called` events in that time window
3. Hope no other run overlapped that window

With `run_id` on every event, step 1–3 collapse to a single filter: `run_id == Y`.

The docs/concepts/events.md design doc already describes `run_id` as a stable envelope
field. This FP closes the gap between the documented design and the implementation.

### Isolation from crash recovery

These are pure observability changes. The WAL (`state_log.jsonl`) handles crash recovery
independently; none of the emit() calls touch the WAL. Adding kwargs to emit() has zero
risk to recovery correctness.

---

## Proposed implementation

### Seven targeted changes (all in existing call sites)

**1. `workflow_finished`** — `src/reyn/kernel/runtime.py`

```python
# Before
self.events.emit("workflow_finished",
    phase=phase, reason=reason, confidence=confidence,
    total_phase_count=..., final_output_keys=...)

# After
self.events.emit("workflow_finished",
    run_id=self.run_id, skill=self.skill.name,
    phase=phase, reason=reason, confidence=confidence,
    total_phase_count=..., final_output_keys=...)
```

**2. `llm_called`** — `src/reyn/kernel/runtime.py`

```python
# Before
self.events.emit("llm_called", phase=phase, model=resolved_model)

# After
self.events.emit("llm_called",
    run_id=self.run_id, skill=self.skill.name,
    phase=phase, model=resolved_model)
```

**3. `llm_response_received`** — `src/reyn/kernel/runtime.py`

```python
# Before
self.events.emit("llm_response_received",
    phase=phase, response_type=..., raw=raw, ...)

# After
self.events.emit("llm_response_received",
    run_id=self.run_id, skill=self.skill.name,
    phase=phase, response_type=..., raw=raw, ...)
```

**4. `permission_denied`** — `src/reyn/op_runtime/__init__.py`

```python
# Before
ctx.events.emit("permission_denied", kind=op.kind, path=path, reason=str(exc))

# After
ctx.events.emit("permission_denied",
    run_id=ctx.run_id, skill=ctx.skill_name, phase=ctx.current_phase,
    kind=op.kind, path=path, reason=str(exc))
```

Note: `OpContext` may need `run_id` and `skill_name` fields added if not already present.
Check `src/reyn/op_runtime/context.py`.

**5. `user_intervention_requested`** — `src/reyn/op_runtime/ask_user.py`

```python
# Before
ctx.events.emit("user_intervention_requested",
    phase=ctx.current_phase, question=op.question, suggestions=op.suggestions or [])

# After
ctx.events.emit("user_intervention_requested",
    run_id=ctx.run_id, skill=ctx.skill_name,
    phase=ctx.current_phase, question=op.question,
    intervention_id=iv.id,          # ← enables correlation with received
    suggestions=op.suggestions or [])
```

**6. `user_intervention_received`** — `src/reyn/op_runtime/ask_user.py`

```python
# Before
ctx.events.emit("user_intervention_received", phase=ctx.current_phase, answer=text)

# After
ctx.events.emit("user_intervention_received",
    run_id=ctx.run_id, skill=ctx.skill_name,
    phase=ctx.current_phase, answer=text,
    intervention_id=iv.id)          # ← correlates back to requested
```

**7. `permission_granted` (new event)** — `src/reyn/op_runtime/__init__.py` or
`src/reyn/op_runtime/dispatcher.py`

Add an emit on the allow path, symmetric with `permission_denied`:

```python
ctx.events.emit("permission_granted",
    run_id=ctx.run_id, skill=ctx.skill_name, phase=ctx.current_phase,
    kind=op.kind, path=path)
```

Target location: immediately before or after the op is dispatched, once the permission
check passes.

---

## Target files

| File | Change |
|---|---|
| `src/reyn/kernel/runtime.py` | Add `run_id`, `skill` to `workflow_finished`, `llm_called`, `llm_response_received` |
| `src/reyn/op_runtime/__init__.py` | Add `run_id`, `skill`, `phase` to `permission_denied`; add `permission_granted` |
| `src/reyn/op_runtime/ask_user.py` | Add `run_id`, `skill`, `intervention_id` to both intervention events |
| `src/reyn/op_runtime/context.py` | Add `run_id` and `skill_name` to `OpContext` if missing |
| `docs/concepts/events.md` | Correct `kind` → `type`; note `run_id` is now consistently present |

---

## Dependencies

- None. All call sites already have access to `run_id` and `skill` (via `self` in
  `OSRuntime`, via `ctx` in op_runtime). No structural changes needed.

---

## Cost estimate

| Task | Cost |
|---|---|
| Add `run_id`/`skill` to 3 runtime emit calls | SMALL |
| Add `run_id`/`skill`/`phase` to permission_denied | SMALL |
| Add `permission_granted` event | SMALL |
| Add `run_id`/`skill`/`intervention_id` to intervention events | SMALL |
| Update `docs/concepts/events.md` | SMALL |
| **Total** | **SMALL** |

All changes are additive kwargs — no existing consumers break (they already ignore unknown
data fields).

---

## Related

- `src/reyn/kernel/runtime.py` — `workflow_finished`, `llm_called`, `llm_response_received`
- `src/reyn/op_runtime/__init__.py` — `permission_denied`
- `src/reyn/op_runtime/ask_user.py` — `user_intervention_requested/received`
- `docs/concepts/events.md` — design doc describing `run_id` as stable envelope field
- FP-0018 (`0018-event-store-backend.md`) — future backend abstraction; this FP's richer
  events will be more useful once queryable via SQLite/DuckDB backend
- FP-0007 (`0007-evaluation-infrastructure.md`) — eval trace export benefits directly
  from `run_id`-correlated `llm_called` events
