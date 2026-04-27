# app_builder — Generate an app from natural language

Takes a natural language description and auto-generates a complete set of DSL files for a new app that runs on Reyn.

---

## What it does

- Describe your app's purpose in plain English (or any language) and get a full phase/artifact/graph design
- Generated files are immediately runnable with `reyn run`
- Automatically determines whether a review loop is needed and selects the appropriate phase structure

---

## Usage

```bash
reyn run \
  --app-dsl src/stdlib/apps/app_builder/app.md \
  --dsl-root src/stdlib \
  --model openai/gemini-2.5-flash-lite \
  --input "Describe the app you want to build"
```

Generated files are written to `workspace/dsl/apps/{app_name}/`.

---

## Input format

Describe the app's purpose and functionality in natural language.

**Minimum required information:**
- What the app does
- What the input and output look like

**Examples:**

```
Build an app that auto-generates blog articles.
Given a topic, it drafts an article, runs a quality review, and outputs the final version.
```

```
An app that receives customer feedback text and classifies it into
positive, negative, and suggestion categories with a summary.
```

If you are unsure about the app name, write "suggest some name options" and the builder will propose candidates.

---

## Phase flow

```
plan_app  →  build_app
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `plan_app` | app_architect | Designs the app structure: phases, artifacts, and transition graph |
| `build_app` | dsl_writer | Generates and writes DSL files based on the design |

---

## Output file structure

Files are written inside the workspace:

```
workspace/dsl/apps/{app_name}/
  app.md                   ← App definition (entry phase, graph, final output)
  phases/
    {phase_name}.md        ← Per-phase definition
  artifacts/
    {artifact_name}.md     ← Per-artifact schema
```

---

## Final output

```json
{
  "app_name": "my_app",
  "app_path": "dsl/apps/my_app",
  "files_written": [
    "dsl/apps/my_app/app.md",
    "dsl/apps/my_app/phases/analyze.md",
    "dsl/apps/my_app/artifacts/analysis_result.md"
  ],
  "file_count": 5,
  "summary": "An app that ..."
}
```

---

## After generation

Generated files are written inside the **workspace** (`workspace/dsl/apps/{app_name}/`).
Reyn never writes outside the workspace, so you need to copy them into your project.

1. Copy into your project's `dsl/` directory:

   ```bash
   cp -r workspace/dsl/apps/{app_name} dsl/apps/
   ```

2. Run it:

   ```bash
   reyn run --app-dsl dsl/apps/{app_name}/app.md --dsl-root dsl/ --input "test input"
   ```

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
- **Linting runs automatically**: `app_builder` includes a lint step — check the final output's `lint_result` for any issues
- **Low quality? Use app_improver**: pass the generated app to `app_improver` to auto-improve phase instructions
