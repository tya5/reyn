---
type: phase
name: plan_app
input: user_message | app_request
role: app_architect
---

Design an App structure that fulfills the user's request with appropriate quality controls.

SCOPE BOUNDARY — CRITICAL:
Your job is to design the TARGET app (the one the user wants built).
Any meta-instructions from the user (e.g. "suggest an app name", "ask me for details") are
addressed HERE by YOU — they are NOT requirements for the target app's phases.
Do NOT embed app-builder concerns (naming, clarification) into the target app's phase instructions.

---

## Step 0 — Discover available MCP servers (when relevant)

If the user's request implies accessing **external systems** — such as GitHub, databases, web search,
Slack, email, calendars, Git, file systems, or any 3rd-party API — search the GitHub MCP Registry
before designing the phases:

```
run_skill: mcp_search
input: <the user's original request text>
slot: mcp_results
```

The `mcp_results` slot will contain an `mcp_candidate_list` with matching servers.
From those candidates, select 0–3 that would most benefit this skill and record them in `mcp_servers`.
If no candidates are relevant, set `mcp_servers: []`.

Skip this step entirely if the skill is self-contained (text processing, classification,
document generation with no external data needs).

---

## Step 1 — Check for naming conflicts

Glob `reyn/local/` to list existing apps. If `reyn/local/{app_name}` already exists,
use ask_user to inform the user and ask whether to choose a different name or overwrite.
Proceed only after confirming the app_path is safe to use.

---

## Step 2 — Choose a quality pattern

Before laying out phases, identify what "quality" means for this app's output.
Then choose the appropriate pattern:

### Pattern A — Linear with review
Use when: the task generates content or artifacts that need quality assessment.
```
generate → review  (review has can_finish: true; on reject, OS rollback re-runs generate at runtime — NOT a graph edge)
```

CRITICAL — even if the user says "revise", "loop back", "iterate until approved", do NOT create a
separate `revise` phase. Revision is the SAME `generate` phase re-executed by the OS rollback
mechanism with the reviewer's feedback as input. Only `generate` and `review` phases exist in
this pattern.

### Pattern B — Research then generate
Use when: the task benefits from gathering information before generating.
```
research → generate → review
```

### Pattern C — Simple linear (no review)
Use when: the task is deterministic or structurally well-defined with no ambiguity.
```
process → deliver
```

Choose the simplest pattern that achieves sufficient output quality.
Do NOT add review phases unless the output is subjective or hard to verify.

---

## Step 3 — Define structure

app_name: snake_case name of the target app
app_description: one sentence describing what the app does (used in `reyn apps` listing)
app_path: "reyn/local/{app_name}"
entry_phase: name of the first phase
finish_criteria: 2–4 bullet strings describing when the TARGET workflow is done

phases: array of phase definitions, each with:
  - name: snake_case phase name
  - role: the LLM role for this phase (e.g. "analyzer", "writer", "reviewer")
  - model_class: one of "light" | "standard" | "strong":
      light    — simple structuring, formatting, deterministic extraction
      standard — main generation, analysis, most phases (default when uncertain)
      strong   — complex multi-criteria reasoning, nuanced review, high-stakes decisions
  - input_artifact: name of the artifact this phase receives
  - instructions: 2–4 sentence domain-logic instructions for the TARGET app's task only.
      For review phases: specify concrete quality criteria, verdict fields, and when to approve vs. request revision.
  - can_finish: true only if this phase may end the workflow

transitions: array of {from: phase_name, to: [phase_name, ...]}
  - `to` values MUST be phase names only — NEVER artifact names.
  - A phase with `can_finish: true` terminates the workflow and MUST have an empty `to: []` (no outgoing edge).
  - Every phase except the entry phase MUST appear as a destination in at least one transition edge.

CRITICAL — graph is a DAG (no cycles, no back-edges):
The graph expresses ONLY forward flow. Revision loops are handled at runtime by the OS via
`control.type='rollback'` — NOT by graph edges. Writing a back-edge from review to an earlier
phase will fail the linter.

WRONG: {from: "review", to: ["generate"]}   ← back-edge creates cycle generate→review→generate
RIGHT: {from: "review", to: []}             ← review has can_finish: true; rollback handled at runtime

artifacts: list of artifact names and descriptions only — NO schemas yet.
  - name: snake_case artifact name (matches a phase's input_artifact)
  - description: one sentence describing what this artifact contains and its purpose

CRITICAL — artifact coverage rule:
Every input_artifact in ANY phase MUST appear in this artifacts list.
The only exception is `user_message` — it is a stdlib artifact and must NOT be redefined here.
If the entry phase accepts natural language input, its input_artifact MUST be `user_message`.

final_output:
  - name: snake_case name for the final output artifact
  - description: one sentence describing it

---

## Design principles

- Each phase does exactly one thing
- Artifact names must be unique and consistent across phases and transitions
- Phase instructions must describe the target app's domain logic ONLY
- Review phase instructions MUST specify: what criteria to evaluate, and when to approve vs. request revision
- Review phase instructions MUST include: "If rejected, emit `control.type='rollback'` with a reason explaining what to fix."
- CRITICAL — the artifact a review phase receives must contain all information needed to make an informed judgment. Design the intermediate artifact so the reviewer is self-contained — do not assume it can infer context from prior phases.
