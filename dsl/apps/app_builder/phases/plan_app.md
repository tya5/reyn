---
type: phase
name: plan_app
input: user_message | app_request
input_description: Either a natural language request (user_message.data.text) or a structured app_request (data.app_name, data.description, data.goal). If critical fields are missing, use ask_user to collect them before producing the plan.
role: app_architect
---

Design a minimal App structure that fulfills the user's request.

SCOPE BOUNDARY — CRITICAL:
Your job is to design the TARGET app (the one the user wants built).
Any meta-instructions from the user (e.g. "suggest an app name", "ask me for details") are
addressed HERE by YOU — they are NOT requirements for the target app's phases.
Do NOT embed app-builder concerns (naming, clarification) into the target app's phase instructions.

app_name:
- If the input is an app_request, use data.app_name.
- If the input is a user_message, infer a snake_case app name from the request.
- If the user asked for name suggestions, use ask_user to present 2–3 candidates and let them choose BEFORE producing the plan.

Produce a structured plan with the following shape:

app_name: snake_case name of the target app
app_path: "dsl/apps/{app_name}"
entry_phase: name of the first phase
finish_criteria: 2–4 bullet strings describing when the TARGET workflow is done

phases: array of phase definitions, each with:
  - name: snake_case phase name
  - role: the LLM role for this phase (e.g. "analyzer", "writer", "reviewer")
  - input_artifact: name of the artifact this phase receives
  - input_description: one sentence describing the input artifact's fields and purpose
  - instructions: 2–4 sentence domain-logic instructions for the TARGET app's task only
  - can_finish: true only if this phase may end the workflow

transitions: array of {from: phase_name, to: [phase_name, ...]}
  - The last phase in the happy path must be can_finish: true

artifacts: array of artifact definitions, each with:
  - name: snake_case artifact name (matches a phase's input_artifact)
  - fields: array of {name, type} where type is string | integer | number | boolean | array | object

final_output:
  - name: snake_case name for the final output artifact
  - description: one sentence describing it
  - fields: same structure as artifact fields

Design principles:
- Prefer 2–3 phases unless the task clearly requires more
- Each phase does exactly one thing
- Artifact names must be unique and consistent across phases and transitions
- Phase instructions must describe the target app's domain logic ONLY — never meta-tasks like naming or clarification
