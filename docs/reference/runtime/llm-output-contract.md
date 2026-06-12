---
type: reference
topic: runtime
audience: [human, agent]
---

# LLM output contract

Every phase, regardless of skill, expects the LLM to return a single JSON object matching this schema. Output that doesn't conform is rejected — the OS surfaces a `validation_error` event and re-prompts (subject to retry limits) or fails the phase.

## Schema

```json
{
  "control": {
    "type": "transition | finish | abort | rollback",
    "decision": "continue | finish | abort",
    "next_phase": "<phase_name> or null",
    "confidence": 0.0,
    "reason": {"summary": "one-sentence explanation"}
  },
  "artifact": {
    "type": "<schema_name>",
    "data": { ... }
  },
  "ops": []
}
```

## `control` block

### `type`

The shape of the transition the LLM is requesting.

- `transition` — go to another phase.
- `finish` — terminate the workflow cleanly. The artifact must match the skill's `final_output_schema`.
- `abort` — unrecoverable error. The artifact may be empty.
- `rollback` — send the immediately preceding phase back for revision. The OS determines the rollback target automatically. `next_phase` must be `null`. Set `decision` to `continue` by convention; the OS does not enforce a specific `decision` value for rollback. The artifact may be empty; put the rejection reason in `reason.summary`.

### `decision`

OS-level intent. **Only three values are valid.** Skill-specific verbs like `revise` are NOT allowed (P7).

- `continue` — normal transition. Valid for any non-terminal flow, including revision loops where you transition to a `revise` phase.
- `finish` — terminate. Requires `type=finish` and `next_phase=null`.
- `abort` — error termination. Requires `type=abort` and `next_phase=null`.

### `next_phase`

- For `type=transition`, must be one of the allowed next phases for the current phase, per the skill graph.
- For `type=finish` or `type=abort`, must be `null`.

### `confidence`

Float in `[0.0, 1.0]`. Used for telemetry; does not affect dispatch.

### `reason.summary`

One-sentence rationale. Stored in the event log.

## `artifact` block

- `type` — the artifact schema name. Must match either the input schema of `next_phase` (for transitions) or the skill's `final_output_schema` (for finish).
- `data` — the artifact payload. Validated against the schema.

In `--strict` mode, required fields are enforced at every nesting level. In default lenient mode, only top-level required fields are enforced.

## `ops` block

A list of side-effect ops. Each op is dispatched in order. See [control-ir.md](control-ir.md).

## Consistency rules

These are checked before dispatch. Violations are rejected.

- `type=finish` ⇔ `decision=finish` ⇔ `next_phase=null`.
- `type=transition` ⇔ `decision=continue` ⇔ `next_phase` is a non-null, allowed phase.
- `type=abort` ⇔ `decision=abort` ⇔ `next_phase=null`.
- `type=rollback` → `next_phase=null` (enforced). `decision=continue` is the recommended convention but is not checked by the OS.
- `artifact.type` matches the schema implied by the chosen target.

## Why this contract is rigid

The OS's job is to make LLM-driven control flow safe. By rejecting any output that hallucinates a phase name, invents a decision verb, or returns a malformed artifact, reyn prevents the runtime from drifting into states the Skill author didn't anticipate. The LLM is free to be creative *inside* the artifact — never about *which artifact* or *which phase*.

## See also

- [context-frame.md](context-frame.md) — what the LLM sees
- [control-ir.md](control-ir.md) — Control IR op schemas
- [Concepts: principles P4](../../concepts/architecture/principles.md#p4-llm-is-a-constrained-decision-engine)
