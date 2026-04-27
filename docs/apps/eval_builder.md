# eval_builder — Auto-generate an eval spec for an app

Reads an existing app's DSL files and auto-generates an `eval.md` spec that evaluates each phase's output. The generated spec can be re-run repeatedly with `reyn eval`.

---

## What it does

- Designs schema validation and LLM quality criteria for every phase of the app
- Auto-designs 1–2 test cases (typical case, and a case that triggers a review loop)
- Writes cross-phase consistency checks (e.g. name decided in `plan` must match in `build`)
- Writes the generated `eval.md` to the workspace and prints the run command

---

## Usage

```bash
reyn run eval_builder "DSL path of the app you want to evaluate" \
  --model openai/gemini-2.5-flash-lite
```

---

## Input format

Provide a sentence that includes the target app's `app.md` path.

**Examples:**

```
Create an eval.md for dsl/apps/writing_review_app/app.md
```

```
Generate an eval spec for dsl/apps/architecture_analyzer/app.md.
Focus on the article quality evaluation phase.
```

If the path is unclear, `ask_user` will request it.

---

## Phase flow

```
analyze_app  →  write_eval
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `analyze_app` | eval_designer | Reads all DSL files and designs evaluation criteria per phase |
| `write_eval` | spec_writer | Formats the criteria into `eval.md` and writes it to the workspace |

---

## Generated eval.md structure

```yaml
---
type: eval
app: dsl/apps/my_app/app.md
dsl_root: dsl/
judge_model: openai/gemini-2.5-flash-lite
---

## case: typical_case
input: "Typical test input for this app"

### phase: analyze
schema:
- analysis_result.issues: array, min 1
- analysis_result.score: number, range 0.0-10.0

quality:
- Each issue contains a concrete improvement suggestion

### cross_phase
- plan_app.app_name == build_app.app_name

### final
schema:
- app_name: string
- files_written: array, min 1

quality:
- summary describes the app's purpose from the user's perspective
```

---

## Criterion types

### schema (deterministic validation)

Checks artifact field structure without an LLM — fast and stable.

| Example | Meaning |
|---------|---------|
| `field: string` | Field exists and is a string |
| `field: array, min 1` | Array with at least one item |
| `field: number, range 0.0-10.0` | Number within range |
| `field: boolean, equals true` | Value is exactly true |

### quality (LLM-judged)

The `judge_model` reads and evaluates the content.

```
- Each issue contains a concrete improvement suggestion
- summary describes the app's purpose from the user's perspective
```

**`[aspirational]` tag**: for checks that track capability trends rather than pass/fail requirements. Excluded from the score; treated as informational.

```
- [aspirational] Feedback contains highly specific, actionable suggestions
```

---

## Output file and running

```
workspace/eval_specs/{app_name}/eval.md
```

Copy to your project DSL and run:

```bash
# Copy from workspace to DSL directory
cp workspace/eval_specs/{app_name}/eval.md dsl/apps/{app_name}/eval.md

# Run the eval
reyn eval --spec dsl/apps/{app_name}/eval.md --model openai/gemini-2.5-flash-lite
```

---

## Final output

```json
{
  "eval_md_path": "eval_specs/my_app/eval.md",
  "case_count": 2,
  "total_criteria": 18,
  "next_steps": "Written to workspace/eval_specs/my_app/eval.md. ..."
}
```

---

## Tips

- **Reads all artifact files before designing criteria**: `analyze_app` reads every DSL file first, which prevents references to non-existent fields
- **Field names must match the DSL exactly**: schema assertion paths that don't match the artifact definition will cause eval errors
- **Case 2 matters for review-loop apps**: set an input that is likely to require revision in the first draft to verify the loop works correctly
- **Combine with app_improver**: build a spec with `eval_builder` → improve with `app_improver` → measure with `eval` — a tight quality improvement cycle
