---
type: phase
name: plan_app
input: user_message | app_request
input_description: Either a natural language request (user_message.data.text) or a structured app_request (data.app_name, data.description, data.goal). If critical fields are missing, use ask_user to collect them before producing the plan.
role: app_architect
---

Design a minimal App structure that fulfills data.description and data.goal.

Produce a structured plan with the following shape:

app_name: use data.app_name (snake_case)
app_path: "dsl/apps/{app_name}"
entry_phase: name of the first phase
finish_criteria: 2–4 bullet strings describing when the workflow is done

phases: array of phase definitions, each with:
  - name: snake_case phase name
  - role: the LLM role for this phase (e.g. "analyzer", "writer", "reviewer")
  - input_artifact: name of the artifact this phase receives
  - input_description: one sentence describing the input artifact's fields and purpose
  - instructions: 2–4 sentence domain-logic instructions (no output field listings, no Control IR format)
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
