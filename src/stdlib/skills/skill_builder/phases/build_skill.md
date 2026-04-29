---
type: phase
name: build_skill
input: skill_plan
role: dsl_writer
---

Generate DSL markdown files for the skill defined in data, then write each one to the workspace using file ops.

## Step 0 — If re-entered after rollback: check the feedback first

If you are receiving rollback feedback (i.e., a previous build was rejected by a downstream phase), inspect the feedback BEFORE writing any files.

Classify the feedback:

- **Fixable by rebuilding files** — concrete file-level issues you can act on without changing the plan:
  - Missing file, wrong filename, malformed frontmatter, missing fields in an artifact YAML, copy-paste errors in instructions
  - → Proceed to Step 1 and rewrite the affected files (or all files) using the original `skill_plan`.

- **Structural defect in the plan itself** — issues that originate upstream and CANNOT be fixed by rewriting files:
  - Graph cycle (back-edge in transitions)
  - An artifact referenced by a phase's `input` is not declared in `data.artifacts` and is not `user_message`
  - Schema inconsistency between phases (e.g., phase B expects field X but phase A's output schema doesn't have it)
  - Plan structure violates the DAG/no-cycle rule
  - → Do NOT write any files. Emit `control.type="rollback"` with a `reason.summary` that quotes the upstream feedback verbatim and identifies which part of the plan is structurally wrong. The OS will roll back further to the phase that produced the plan.

Rule of thumb: if your only way to make the lint pass would be to **change `skill_plan.transitions`, `skill_plan.artifacts`, or any phase's `input_artifact`**, the fix is upstream — chain the rollback. You only ever transcribe the plan; you never amend it.

## Step 1 — Generate the skill files

CRITICAL: Every file MUST start with `---` and end the frontmatter block with `---`. Missing delimiters will break the parser.

skill.md (write to {skill_path}/skill.md):
```
---
type: skill
name: {skill_name}
description: {skill_description}
entry: {entry_phase}
final_output: {final_output.name}
final_output_description: {final_output.description}
finish_criteria:
  - {criterion1}
  - {criterion2}
graph:
  {phase_a}: [{phase_b}]
  {phase_b}: [{phase_c}, {phase_d}]
---

## 概要
{skill_descriptionの散文説明}

## 入力
{入力に期待する内容と例}
```

If `data.mcp_servers` is non-empty, append a `## MCP` section after `## 入力`:
```
## MCP

このスキルは以下の MCP サーバと連携することで機能が強化されます。
設定方法: `.reyn/config.yaml` の `mcp.servers` に追加してください。

| Server | Purpose |
|--------|---------|
| {name} | {purpose} |
```

graph comes from data.transitions. Each `{from: X, to: [Y, Z]}` entry becomes:
```yaml
X: [Y, Z]
```
Review loops are NOT graph edges. Example forward-only graph for a generate→review pattern:
```yaml
graph:
  generate: [review]
  review: []        # can_finish: true, no outgoing edge
```
(The "loop back to generate" happens at runtime when review emits `control.type="rollback"`.)

CRITICAL — no "finish" node: Do NOT add a `finish` node to the graph.
Workflow termination is expressed by `can_finish: true` on the phase that delivers the final output.

CRITICAL — skip edges to final_output: If any transition target equals data.final_output.name, OMIT that edge.
The final_output artifact is NOT a phase. Only emit edges where the target is a phase listed in data.phases.

CRITICAL — graph must be a DAG (no cycles): Do NOT write back-edges (e.g. review → generate).
Review/revise loops are handled by OS rollback — the review phase emits control.type="rollback" at runtime.
The graph only expresses forward flow. Any cycle will fail the linter.

CRITICAL — do NOT write a user_message artifact file: `user_message` is a stdlib artifact.
If the entry phase accepts `user_message` as input, simply reference it in the phase frontmatter — do not create an artifact file for it.

phase file (write to {skill_path}/phases/{phase_name}.md):
```
---
type: phase
name: {phase_name}
input: {input_artifact}
role: {role}
model_class: {model_class}
can_finish: true
---

{instructions text verbatim}
```
Omit `can_finish` line if the phase cannot finish.
Omit `model_class` line if the phase should use the runtime default (standard).

For review phases: instructions MUST contain both:
1. The specific criteria to evaluate (faithfulness, completeness, quality, etc.)
2. The explicit rejection clause: "If the content does not meet the criteria, emit `control.type='rollback'` with a reason describing what to fix."
Without the rollback clause the review phase approves everything and the revision loop never triggers.

artifact file (write to {skill_path}/artifacts/{artifact_name}.yaml):
```
name: {artifact_name}
description: {artifact_description}
schema:
  type: object
  properties:
    {field_name}:
      type: {json_schema_type}
      description: {field_description}
    {field_name}:
      type: array
      items:
        type: string
      description: {field_description}
  required: [{required_field1}, {required_field2}]
```
Always include `type: object`, `properties`, `required`, and a `description` on every field.
Use the schema exactly as defined in data.artifacts[].schema and data.final_output.schema.
Artifact files are plain YAML — no frontmatter delimiters.

IMPORTANT: Write ALL artifact files — including the final_output artifact.
Checklist before finishing:
- skill.md written
- one phase file per phase in data.phases
- one artifact file per artifact in data.artifacts (all fields have descriptions and explicit types)
- one artifact file for data.final_output (using data.final_output.name as filename)
- every phase's `input:` field resolves to either a written artifact file, `user_message` (stdlib), or data.final_output.name — if any phase's input is missing, STOP and write the missing artifact file before proceeding

Write all files using one op per file. Once all files are written, finish with a `build_result` artifact:
- `skill_name`: the generated skill name
- `skill_path`: workspace-relative path (e.g. "reyn/local/my_app")
- `files_written`: list of all file paths written
- `file_count`: total number of files
