# app_improver — Automatically improve an existing app

Runs the target app, analyzes its execution log and artifacts, and generates concrete DSL improvements.

---

## What it does

- Analyzes phase visit counts, validation errors, and confidence scores to identify quality issues
- Inspects artifact content for missing fields or thin output
- Generates concrete patches to phase instructions and artifact schemas
- Writes improved files to the workspace and provides copy instructions

---

## Usage

```bash
reyn run app_improver "Information about the app you want to improve" \
  --model openai/gemini-2.5-flash-lite \
  --allow-shell
```

> **Note**: `--allow-shell` is required because the `run_target` phase executes the target app as a subprocess.

---

## Input format

Provide the following information in natural language:

| Field | Required | Description |
|-------|----------|-------------|
| App DSL path | yes | Path in the form `dsl/apps/{app_name}/app.md` |
| Test input | yes | Input text to pass to the target app |
| Improvement focus | optional | Specific area to focus on, e.g. review phase quality |
| Model | optional | Defaults to the currently running model |

**Example:**

```
Please improve dsl/apps/writing_review_app/app.md.
Test input: "Write an article about the future of AI"
Focus on the feedback quality of the review phase.
Model: openai/gemini-2.5-flash-lite
```

---

## Phase flow

```
prepare → run_target → analyze_execution → plan_improvements → apply_improvements
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `prepare` | meta_coordinator | Parses input and prepares execution parameters for the target app |
| `run_target` | executor | Runs the target app as a subprocess and captures log and artifact paths |
| `analyze_execution` | quality_analyst | Inspects event log, artifacts, and DSL files; identifies quality issues with scores |
| `plan_improvements` | app_architect | Designs concrete DSL changes for each identified issue |
| `apply_improvements` | implementer | Writes patched files to `dsl_patches/` in the workspace |

---

## Output and applying improvements

Improved files are written inside the workspace. Applying them to your project DSL is a manual step.

```
workspace/dsl_patches/
  apps/{app_name}/
    phases/{phase_name}.md    ← improved phase definition
    artifacts/{name}.md       ← improved artifact schema
```

To apply:

```bash
# Review the patch
cat workspace/dsl_patches/apps/{app_name}/phases/{phase_name}.md

# Copy into your project DSL
cp workspace/dsl_patches/apps/{app_name}/phases/{phase_name}.md \
   dsl/apps/{app_name}/phases/{phase_name}.md
```

Final output example:

```json
{
  "files_modified": [
    "dsl_patches/apps/writing_review_app/phases/review.md → dsl/apps/writing_review_app/phases/review.md"
  ],
  "summary": "Clarified review phase instructions; added explicit evaluation criteria and field semantics for the verdict field",
  "next_steps": "Review files in workspace/dsl_patches/ and copy them to the target paths if they look good"
}
```

---

## What analyze_execution looks at

From the execution log (JSONL):

| Signal | What it means |
|--------|---------------|
| Phase visit count | High count → LLM is struggling with the instructions |
| `phase_retry` events | Validation failures → usually caused by ambiguous phase instructions |
| Confidence score | Low score → LLM is uncertain about its decision |
| Artifact content richness | Detects missing required fields or thin output |
| `workflow_aborted` | Presence of fatal errors |

If the quality score is **8 or above**, no changes are made (empty `changes` array is returned).

---

## Tips

- **Use realistic test input**: overly simple input may not surface real problems
- **Specifying a focus area improves precision**: e.g. "review quality", "artifact field design"
- **Measure improvement with eval_builder**: build a test spec with `eval_builder` and verify the improvement numerically
- **Target workspace is created automatically**: execution results are saved to `workspace/target_runs/{app_name}/`
