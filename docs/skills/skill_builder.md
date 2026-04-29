# skill_builder — Generate a skill from natural language

Takes a natural-language description and auto-generates a complete set of DSL files for a new skill that runs on Reyn.

---

## What it does

- Describe a skill's purpose in plain language and get a full phase/artifact/graph design
- Generated files are immediately runnable with `reyn run`
- Selects the appropriate phase pattern (review-loop / research-first / linear) automatically
- Lints the generated DSL before reporting success

---

## Usage

```bash
reyn run skill_builder "Describe the skill you want to build"
```

Generated files are written to `reyn/local/{skill_name}/`.

---

## Input format

Describe the skill's purpose and functionality in natural language.

**Minimum required information:**
- What the skill does
- What the input and output look like

**Examples:**

```
Build a skill that auto-generates blog articles.
Given a topic, draft an article, run a quality review, and output the final version.
```

```
A skill that receives customer feedback text and classifies it into
positive, negative, and suggestion categories with a summary.
```

If you are unsure about the skill name, write "suggest some name options" and the builder will propose candidates.

---

## Phase flow

```
plan_app  →  design_artifacts  →  review_plan  →  build_app  →  verify_app
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `plan_app` | architect | Designs phases, transitions, and the overall structure |
| `design_artifacts` | schema_designer | Adds JSON Schemas to each artifact and the final output |
| `review_plan` | reviewer | Sanity-checks the plan before any files are written |
| `build_app` | dsl_writer | Writes `skill.md`, `phases/*.md`, and `artifacts/*.yaml` |
| `verify_app` | verifier | Runs the linter; rolls back to `build_app` if errors are found |

The OS preprocessor injects deterministic structural lint hints into `design_artifacts` (graph cycles, transition targets, artifact coverage, entry-phase validity) so issues surface before file generation.

---

## Output file structure

Files are written under `reyn/local/{skill_name}/`:

```
reyn/local/{skill_name}/
  skill.md                 ← Skill definition (entry, graph, final_output)
  phases/
    {phase_name}.md        ← Per-phase definition
  artifacts/
    {artifact_name}.yaml   ← Per-artifact JSON Schema
```

Run the generated skill directly by name:

```bash
reyn run {skill_name} "test input"
```

---

## Final output

```json
{
  "type": "skill_builder_result",
  "data": {
    "app_name": "my_skill",
    "app_path": "reyn/local/my_skill",
    "files_written": [
      "reyn/local/my_skill/skill.md",
      "reyn/local/my_skill/phases/analyze.md",
      "reyn/local/my_skill/artifacts/analysis_result.yaml"
    ],
    "file_count": 5,
    "lint_passed": true,
    "lint_issues": [],
    "summary": "A skill that ..."
  }
}
```

When `lint_passed: false`, the `lint_issues` array lists the remaining problems and `verify_app` will have rolled back to `build_app` to retry — only a final, irrecoverable failure surfaces here.

---

## Phase design patterns

`plan_app` selects the best pattern based on the input:

| Pattern | Structure | Best for |
|---------|-----------|----------|
| A: Review loop | generate → review → deliver | Content generation requiring subjective judgment |
| B: Research first | research → generate → review → deliver | When information gathering must precede generation |
| C: Simple linear | process → deliver | Deterministic transformations and classification |

---

## Tips

- **Vague input is fine**: missing information is requested via `ask_user`
- **Linting is enforced**: `verify_app` rolls back on errors, so you only see a successful result when the DSL passes lint
- **Low quality? Use [skill_improver](skill_improver.md)**: pass the generated skill to `skill_improver` to auto-improve phase instructions and artifact schemas
