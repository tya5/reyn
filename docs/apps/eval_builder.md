# eval_builder — Auto-generate an eval spec for an app

Reads an existing app's DSL files and auto-generates an `eval.md` spec with
per-phase LLM-judged quality criteria. The generated spec is then run with
`reyn eval`.

---

## What it does

- Reads every phase and artifact file in the target app
- Designs 1–2 representative test cases (typical, plus a rollback case if the app has a review loop)
- Writes 1–4 quality criteria per phase
- Writes the spec to `{app_dir}/eval.md`

---

## Usage

```bash
reyn run eval_builder "Generate an eval.md for reyn/local/my_app/app.md"
```

Then run the generated spec:

```bash
reyn eval reyn/local/my_app/eval.md --model standard
```

---

## Input format

A natural-language sentence including the target app's `app.md` path.

**Examples:**

```
Create an eval.md for reyn/local/article_generator/app.md
```

```
Generate an eval spec for reyn/project/code_analyzer/app.md.
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
| `analyze_app` | eval_designer | Reads DSL files; designs test cases and per-phase quality criteria |
| `write_eval`  | spec_writer   | Formats the criteria into `eval.md` and writes it next to the target app |

---

## Generated eval.md structure

```yaml
---
type: eval
app: reyn/local/my_app/app.md
dsl_root: reyn/
---

## case: typical_input
input: "A realistic user message for this app"

### phase: analyze
quality:
- Each issue contains a concrete improvement suggestion
- [aspirational] Suggestions reference specific lines or code regions

### phase: review
quality:
- Verdict matches the issues listed in the analysis
```

---

## Criterion format

Each criterion is an LLM-judged sentence describing a semantic property
of the phase's output artifact.

- **Required (default):** failure counts against the score.
- **`[aspirational]` tag:** tracked but excluded from pass/fail. Use for
  capability-ceiling criteria or branch-conditional checks.

```
- Each issue contains a concrete improvement suggestion
- [aspirational] Feedback contains highly specific, actionable suggestions
```

The judge model is set in `judge_phase` (the `model_class` of its `judge` phase) — it is not configurable per-spec.

---

## Final output

```json
{
  "eval_md_path": "reyn/local/my_app/eval.md",
  "case_count": 2,
  "criterion_count": 12,
  "summary": "Wrote eval.md with 2 cases × 6 criteria. Run: reyn eval reyn/local/my_app/eval.md"
}
```

---

## Tips

- **Reads all artifact files first**: `analyze_app` reads every artifact `.yaml` before writing criteria, so field names referenced in criteria match what the artifact actually contains.
- **Case 2 matters for review-loop apps**: an input that is likely to require revision in the first draft exercises the rollback path.
- **Combine with app_improver**: build a spec with `eval_builder` → improve with `app_improver` → measure with `reyn eval` — a tight quality improvement cycle.
