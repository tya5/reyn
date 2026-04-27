# Reyn

LLM-driven workflow engine with a Markdown DSL.

Define multi-phase workflows as plain Markdown files. The OS handles LLM calls, routing, validation, and retries — your phases contain only domain logic.

---

## Key concepts

| Concept | Role |
|---------|------|
| **App** | Defines the phase graph and final output schema |
| **Phase** | Reusable processing unit — input schema + instructions only |
| **Artifact** | Typed, structured data passed between phases |
| **OS** | Runtime: builds context, calls LLM, validates output, routes transitions |

Phases are stateless and reusable. They don't know what comes next — the App graph decides that. The OS is app-agnostic: adding a new App never changes OS code.

---

## Installation

```bash
pip install -e ".[rich]"
```

Requires Python 3.11+ and a [LiteLLM](https://github.com/BerriAI/litellm)-compatible model.

Set your API key before running:

```bash
export OPENAI_API_KEY=sk-...
```

---

## Quick start

### 1. Define artifacts

```markdown
<!-- dsl/apps/my_app/artifacts/user_input.md -->
---
type: artifact
name: user_input
wrapped: false
---

topic: string
audience: string
tone?: string
```

```markdown
<!-- dsl/apps/my_app/artifacts/report.md -->
---
type: artifact
name: report
---

title: string
body: string
```

### 2. Define phases

```markdown
<!-- dsl/apps/my_app/phases/draft.md -->
---
type: phase
name: draft
input: user_input
role: writer
---

Write a report on the topic for the given audience.
Use the specified tone if provided; otherwise default to neutral.
```

### 3. Define the app

```markdown
<!-- dsl/apps/my_app/app.md -->
---
type: app
name: my_app
entry: draft
final_output: report
---

draft
```

### 4. Run

```bash
reyn run \
  --app-dsl dsl/apps/my_app/app.md \
  --input "topic: AI in education, audience: university students" \
  --model gpt-4o
```

---

## Artifacts

Artifacts are the data contracts between phases. Define fields with simple type annotations:

```markdown
---
type: artifact
name: article
---

title: string
body: string
tags: string[]
word_count?: integer
```

**Supported types:** `string`, `number`, `integer`, `boolean`, `string[]`, `number[]`, `integer[]`, `object`, `array`, `any`

**Optional fields** are marked with `?`. Artifacts can reference other artifacts as nested objects.

**`wrapped: false`** removes the `{type, data}` envelope — use this for entry-phase inputs where a flat schema is cleaner.

---

## Apps

Apps define the phase graph, transition rules, and final output:

```markdown
---
type: app
name: writing_review_app
entry: analyze
final_output: final_article
finish_criteria:
  - clarity
  - structure
max_phase_visits:
  review: 3
  revise: 3
---

analyze -> draft -> review -> judge
judge -> revise -> review
```

**Graph syntax:** `phase_a -> phase_b` adds a directed edge. Phases reachable from `entry` form the graph. The LLM chooses transitions at runtime.

### App nodes

Apps can embed other apps as graph nodes using `@app_name`:

```markdown
---
type: app
name: digest_pipeline
entry: prepare
final_output: digest_result
---

prepare -> @writing_review_app -> digest
```

The OS runs the sub-app to completion, adapts its output to the next phase's input schema, and continues.

**Workspace isolation:** `@writing_review_app[shared]` shares the parent workspace; default is `isolated`.

---

## Phases

Phases declare an input artifact and instructions. They do not define output schema — that is determined by the next phase's input, or the app's `final_output`.

```markdown
---
type: phase
name: review
input: draft_article
role: reviewer
can_finish: true
max_act_turns: 5
---

Review the draft for clarity, accuracy, and structure.
Approve if it meets quality standards; otherwise list specific improvements needed.
```

**`can_finish: true`** allows the LLM to end the workflow from this phase.

**`max_act_turns`** caps how many tool-use (act) turns the LLM may take before emitting a decision.

**Multi-input phases** accept any of several artifact types:

```markdown
input: draft_article | revised_article
```

---

## Control IR

Within a phase, the LLM can perform side effects before making a routing decision:

```markdown
<!-- Reading a file -->
{"kind": "file", "op": "read", "path": "notes.txt"}

<!-- Writing a file -->
{"kind": "file", "op": "write", "path": "output.md", "content": "..."}

<!-- Glob search -->
{"kind": "file", "op": "glob", "path": "src/**/*.py"}

<!-- Ask the user a question (phase re-runs with the answer) -->
{"kind": "ask_user", "question": "What tone should the article use?", "suggestions": ["formal", "casual"]}

<!-- Run a shell command (requires --allow-shell) -->
{"kind": "shell", "cmd": "python main.py run --app-dsl dsl/apps/foo/app.md --input 'hello'"}
```

---

## CLI reference

### `reyn run`

```
reyn run --app-dsl PATH --input TEXT [options]

  --app-dsl PATH          Markdown App DSL file
  --app MODULE            Python module exposing an 'app' object
  --input TEXT            JSON artifact or natural language string
  --model MODEL           LiteLLM model name (default: gpt-4o)
  --output-language LANG  LLM output language (default: ja)
  --workspace DIR         Workspace directory (default: ./workspace)
  --dsl-root DIR          DSL root for shared artifact/phase resolution
  --strict                Enforce required fields at every nesting depth
  --allow-shell           Enable shell Control IR op
  --read-allow DIR        Allow reading files from DIR (repeatable)
  --rich                  Rich-styled console output
  --events                Print full event log after execution
```

### `reyn eval`

Run an eval spec and score each phase with an LLM judge:

```
reyn eval --spec eval.md --model gpt-4o --judge-model gpt-4o
```

Eval specs define cases and per-phase assertions in Markdown:

```markdown
---
type: eval
app: dsl/apps/my_app/app.md
dsl_root: dsl/
judge_model: gpt-4o
---

## case: basic
input: "Write about AI in education for university students."

### phase: draft
- The article has a clear title
- The body is written for the specified audience

### final
- The body is at least 500 characters
```

### `reyn eval-compare`

Regression check between two eval result JSON files:

```
reyn eval-compare baseline.json candidate.json
```

### `reyn events`

Replay a saved event log:

```
reyn events workspace/runs/20260427T…_my_app.jsonl
reyn events … --conversation   # show LLM context + responses only
reyn events … --filter phase_started --filter phase_completed
```

### `reyn lint` / `reyn format`

```
reyn lint --dsl dsl/
reyn format --dsl dsl/
reyn format --dsl dsl/ --check   # dry-run
```

---

## Architecture

```
User → CLI (reyn run)
         └── Agent
               └── OSRuntime
                     ├── build ContextFrame   (phase + artifact + candidates)
                     ├── call LLM             (via LiteLLM)
                     ├── normalize + validate (output schema, artifact data)
                     ├── execute Control IR   (file / ask_user / shell)
                     ├── store artifact       (Workspace)
                     ├── emit Event           (JSONL log)
                     └── transition → next phase
```

Every state change is recorded as a structured event. Event logs support replay and offline debugging via `reyn events`.

---

## Project structure

```
reyn/       Runtime engine (OS, models, validation, eval)
compiler/       DSL parser, IR, expander, linter, formatter
dsl/
  apps/         App definitions (app.md, phases/, artifacts/)
  shared/       Shared artifacts and phases across apps
examples/       Python-defined app examples
workspace/      Runtime output (artifacts, event logs, eval results)
```

---

## License

MIT — see [LICENSE](LICENSE).
