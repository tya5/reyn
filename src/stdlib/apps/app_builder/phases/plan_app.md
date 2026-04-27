---
type: phase
name: plan_app
input: user_message | app_request
input_description: Either a natural language request (user_message.data.text) or a structured app_request (data.app_name, data.description, data.goal). If critical fields are missing, use ask_user to collect them before producing the plan.
role: app_architect
---

Design an App structure that fulfills the user's request with appropriate quality controls.

SCOPE BOUNDARY — CRITICAL:
Your job is to design the TARGET app (the one the user wants built).
Any meta-instructions from the user (e.g. "suggest an app name", "ask me for details") are
addressed HERE by YOU — they are NOT requirements for the target app's phases.
Do NOT embed app-builder concerns (naming, clarification) into the target app's phase instructions.

app_name:
- If the input is an app_request, use data.app_name.
- If the input is a user_message, infer a snake_case app name from the request.
- If the user asked for name suggestions, use ask_user to present 2–3 candidates and let them choose BEFORE producing the plan.

---

## Quality design (consider this first)

Before laying out phases, identify what "quality" means for this app's output.
Then choose the appropriate quality pattern:

### Pattern A — Linear with review
Use when: the task generates content or artifacts that need quality assessment.
```
generate → review → deliver
```
- `review` has `can_finish: true` and transitions back to `generate` if quality is insufficient.
- The review artifact carries a verdict field (e.g. `approved: boolean`, `feedback: string`).

### Pattern B — Research then generate
Use when: the task benefits from gathering information before generating.
```
research → generate → review → deliver
```

### Pattern C — Simple linear (no review)
Use when: the task is deterministic, lookup-based, or structurally well-defined with no ambiguity.
```
process → deliver
```

Choose the simplest pattern that achieves sufficient output quality.
Do NOT add review phases just to add them — only when the task output is subjective or hard to verify without evaluation.

---

## Plan structure

app_name: snake_case name of the target app
app_description: one sentence describing what the app does (used in `reyn apps` listing)
app_path: ".reyn/dsl/apps/{app_name}"
entry_phase: name of the first phase
finish_criteria: 2–4 bullet strings describing when the TARGET workflow is done

phases: array of phase definitions, each with:
  - name: snake_case phase name
  - role: the LLM role for this phase (e.g. "analyzer", "writer", "reviewer")
  - model_class: one of "light" | "standard" | "strong" — choose based on task complexity:
      light    — simple structuring, formatting, deterministic extraction
      standard — main generation, analysis, most phases (default when uncertain)
      strong   — complex multi-criteria reasoning, nuanced review, high-stakes decisions
  - input_artifact: name of the artifact this phase receives
  - input_description: one sentence describing the input artifact's fields and purpose
  - instructions: 2–4 sentence domain-logic instructions for the TARGET app's task only.
      For review phases: specify concrete quality criteria the reviewer must apply and
      what verdict fields to populate (e.g. approved, score, feedback).
  - can_finish: true only if this phase may end the workflow

transitions: array of {from: phase_name, to: [phase_name, ...]}
  - Review phases that loop back must list BOTH the revision target AND the next phase in `to`.
  - The phase that delivers final output must be can_finish: true.

artifacts: array of artifact definitions, each with:
  - name: snake_case artifact name (matches a phase's input_artifact)
  - fields: array of field objects — EACH field object MUST have BOTH "name" AND "type" keys:
      [{"name": "title", "type": "string"}, {"name": "score", "type": "integer"}]
    Valid types: string | integer | number | boolean | string[] | integer[] | number[] | array | object
    DO NOT use the old {field_name: type} shorthand — always use {"name": "...", "type": "..."}.
    Review artifacts should include verdict fields such as:
      {"name": "approved", "type": "boolean"}, {"name": "feedback", "type": "string"}

CRITICAL — artifact coverage rule:
Every artifact referenced as input_artifact in ANY phase MUST appear in this artifacts array,
INCLUDING the entry phase's input artifact.
The only exception is `user_message` — it is a stdlib artifact and must NOT be redefined here.
If the entry phase accepts natural language input, its input_artifact MUST be `user_message`
(handled by stdlib) — do NOT invent a custom artifact for raw user text.

final_output:
  - name: snake_case name for the final output artifact
  - description: one sentence describing it
  - fields: SAME format as artifact fields — array of {"name": "...", "type": "..."} objects

---

## Design principles

- Each phase does exactly one thing
- Artifact names must be unique and consistent across phases and transitions
- Phase instructions must describe the target app's domain logic ONLY — never meta-tasks like naming or clarification
- Review phase instructions MUST specify: what criteria to evaluate, what the verdict fields mean, and when to approve vs. request revision
