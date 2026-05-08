---
type: reference
topic: runtime
audience: [human, agent]
---

# Context frame

The Context Frame is the read-only payload the OS hands to the LLM at each phase visit. It is regenerated every visit, never persisted, and contains everything the LLM needs to make its next decision.

## Shape

```json
{
  "current_phase": "<phase_name>",
  "current_phase_role": "<role>",
  "instructions": "<phase markdown body>",
  "input_artifact": {"type": "<artifact_type>", "data": { ... }},
  "execution": {
    "path": ["phase_a", "phase_b"],
    "current_visit": 1,
    "total_steps": 3
  },
  "candidate_outputs": [
    {
      "next_phase": "<phase_name> or end",
      "control_type": "transition | finish",
      "schema_name": "<artifact_type>",
      "artifact_schema": { ... },
      "description": "..."
    }
  ],
  "finish_criteria": [],
  "constraints": {
    "max_phase_visits": 25
  },
  "available_control_ops": [
    {"kind": "file", "description": "...", "example": { ... }}
  ],
  "output_language": "en"
}
```

## Fields

### `current_phase`, `current_phase_role`

The name and optional role of the phase currently executing.

### `instructions`

The full markdown body of the phase file. **No frontmatter.** No injected schema info — the body is the human-written guidance verbatim.

### `input_artifact`

The artifact this phase consumes. After preprocessor steps run, the artifact is enriched in place — additional keys (e.g. `relevant_memories`, the output of a `python` step) appear under their declared `into` key.

### `execution`

Trace of the run so far:

- `path` — phases entered, in order.
- `current_visit` — visit count for `current_phase`.
- `total_steps` — total phase visits across the run.

### `candidate_outputs`

The set of OS-allowed transitions from this phase. Each entry includes:

- `next_phase` — the target phase name, or `end` for terminal transitions.
- `control_type` — `transition` or `finish`.
- `schema_name` — the artifact type expected.
- `artifact_schema` — the JSON schema fragment.
- `description` — one-line summary of when to choose this candidate.

The LLM MUST pick one of these. Hallucinated phase names are rejected.

### `finish_criteria`

Free-form bullet list from `skill.md`. Used by phases that decide whether to finish.

### `constraints.max_phase_visits`

Cap on revisits per single phase, from `reyn.yaml` or `--max-phase-visits`. `null` means unlimited.

### `available_control_ops`

The set of Control IR op kinds the LLM may emit, with descriptions and examples. **This is the single source of truth for what ops exist** — phase markdown MUST NOT describe op syntax (P8).

### `output_language`

Target language for natural-language output. From `reyn.yaml` or `--output-language`.

## What's NOT in the frame

- Other phases' artifacts (use `file` ops or sub-skills if you need them).
- The event log.
- Memory or other long-term state (only what was recalled into the input artifact).
- The LLM's own past outputs (each call is stateless — preprocessor + this frame are the only context).

## Why this design

Building a fresh frame every visit forces every phase to be self-contained. There is no hidden conversational state between visits — only what the OS injects. This is what makes runs replayable and individual phases reusable across skills.

## See also

- [llm-output-contract.md](llm-output-contract.md) — the shape of what the LLM returns
- [control-ir.md](control-ir.md) — `available_control_ops` op kinds
- [Concepts: architecture](../../concepts/architecture.md)
