# Reyn

LLM-driven workflow engine with a Markdown DSL.

Define multi-phase workflows as plain Markdown files. The OS handles LLM calls, routing, validation, and retries — your phases contain only domain logic.

---

## Key concepts

| Concept | Role |
|---------|------|
| **Skill** | Defines the phase graph and final output schema |
| **Phase** | Reusable processing unit — input schema + instructions only |
| **Artifact** | Typed, structured data passed between phases |
| **OS** | Runtime: builds context, calls LLM, validates output, routes transitions |

Phases are stateless and reusable. They don't know what comes next — the Skill graph decides that. The OS is skill-agnostic: adding a new Skill never changes OS code.

---

## Installation

```bash
pip install -e ".[rich]"
```

To also enable the **Web UI gateway** (`reyn web`):

```bash
pip install -e ".[web]"
# or both at once:
pip install -e ".[rich,web]"
```

Requires Python 3.11+ and a [LiteLLM](https://github.com/BerriAI/litellm)-compatible model.

Set your API key before running:

```bash
export OPENAI_API_KEY=sk-...
```

Initialize a project (creates `reyn.yaml` and `.reyn/config.yaml`):

```bash
reyn init
```

---

## Quick start

Skills live under `reyn/local/<skill_name>/` (or `reyn/project/<skill_name>/` for shared/checked-in skills). Each skill directory contains `skill.md`, `phases/`, and `artifacts/`.

### 1. Define artifacts

```yaml
# reyn/local/my_skill/artifacts/user_input.yaml
name: user_input
wrapped: false
schema:
  type: object
  properties:
    topic: {type: string}
    audience: {type: string}
    tone: {type: string}
  required: [topic, audience]
```

```yaml
# reyn/local/my_skill/artifacts/report.yaml
name: report
schema:
  type: object
  properties:
    title: {type: string}
    body: {type: string}
  required: [title, body]
```

### 2. Define phases

```markdown
<!-- reyn/local/my_skill/phases/draft.md -->
---
type: phase
name: draft
input: user_input
role: writer
---

Write a report on the topic for the given audience.
Use the specified tone if provided; otherwise default to neutral.
```

### 3. Define the skill

```markdown
<!-- reyn/local/my_skill/skill.md -->
---
type: skill
name: my_skill
entry: draft
final_output: report
graph:
  draft: []
---

## Overview
Drafts a short report from a topic and audience.
```

### 4. Run

```bash
reyn run my_skill '{"type":"user_input","data":{"topic":"AI in education","audience":"university students"}}'
```

Or pass natural language (auto-wrapped as a `user_message` artifact — your entry phase must accept it):

```bash
reyn run my_skill "Write about AI in education for university students."
```

---

## Artifacts

Artifacts are the data contracts between phases. They are defined as YAML files with a JSON Schema body:

```yaml
name: article
schema:
  type: object
  properties:
    title: {type: string}
    body: {type: string}
    tags: {type: array, items: {type: string}}
    word_count: {type: integer}
  required: [title, body]
```

**`wrapped: false`** removes the `{type, data}` envelope — use this for entry-phase inputs where a flat schema is cleaner.

Shared artifacts live under `dsl/shared/artifacts/` (or `<dsl_root>/shared/artifacts/`) and are resolved automatically.

---

## Skills

Skills define the phase graph, transition rules, and final output:

```markdown
---
type: skill
name: writing_review_skill
entry: analyze
final_output: final_article
finish_criteria:
  - clarity
  - structure
graph:
  analyze: [draft]
  draft:   [review]
  review:  [judge]
  judge:   [revise, end]
  revise:  [review]
---

## Overview
Drafts an article, reviews it, and revises until quality is met.
```

The `graph:` mapping declares allowed transitions per phase. The LLM picks one of the listed next-phase candidates at each turn. `end` is a sentinel meaning "this phase may finish the workflow."

### Skill nodes

Skills can embed other skills as graph nodes using `@skill_name`:

```markdown
---
type: skill
name: digest_pipeline
entry: prepare
final_output: digest_result
graph:
  prepare:              [@writing_review_skill]
  "@writing_review_skill": [digest]
  digest:               []
---
```

The OS runs the sub-skill to completion, adapts its output to the next phase's input schema, and continues.

**Workspace isolation:** `@writing_review_skill[shared]` shares the parent workspace; default is `isolated`.

---

## Phases

Phases declare an input artifact and instructions. They do not define output schema — that is determined by the next phase's input, or the skill's `final_output`.

```markdown
---
type: phase
name: review
input: draft_article
role: reviewer
---

Review the draft for clarity, accuracy, and structure.
Approve if it meets quality standards; otherwise list specific improvements needed.
```

**Multi-input phases** accept any of several artifact types:

```markdown
input: draft_article | revised_article
```

**Entry phases** accepting natural language input typically declare:

```markdown
input: user_message | <domain_artifact>
```

---

## Control IR

Within a phase, the LLM can perform side effects before making a routing decision:

```jsonc
// Reading a file
{"kind": "file", "op": "read", "path": "notes.txt"}

// Writing a file
{"kind": "file", "op": "write", "path": "output.md", "content": "..."}

// Glob search
{"kind": "file", "op": "glob", "path": "src/**/*.py"}

// Ask the user a question (phase re-runs with the answer)
{"kind": "ask_user", "question": "What tone should the article use?",
 "suggestions": ["formal", "casual"]}

// Lint a generated skill
{"kind": "lint", "skill_path": "reyn/local/generated_skill"}

// Run another skill as a sub-workflow
{"kind": "run_skill", "skill": "skill_builder", "input": {...}, "into": "result"}

// Run a shell command (requires --allow-shell)
{"kind": "shell", "cmd": "echo hello"}
```

Available ops are injected into each phase's context at runtime; phase instructions never need to enumerate them.

---

## CLI reference

### `reyn run`

```
reyn run [SKILL] [INPUT] [options]

  SKILL                   Skill name. Resolved in order:
                          reyn/project/ → reyn/local/ → src/stdlib/skills/
  INPUT                   JSON artifact or natural language string
                          (omit to read from stdin)

  --skill-path DIR        Path to a skill directory containing skill.md
  --module MODULE         Python module exposing a 'skill' object
  --model MODEL           light | standard | strong, or a LiteLLM model string
  --output-language LANG  LLM output language (default: from reyn.yaml or ja)
  --dsl-root DIR          Override DSL root for shared artifact/phase resolution
  --strict                Enforce required fields at every nesting depth
  --allow-shell           Enable the 'shell' Control IR op
  --max-phase-visits N    Cap visits per phase (0 = unlimited; default 25)
  --rich                  Rich-styled console output
  --events                Print full event log after execution
```

### `reyn skills`

```
reyn skills              # list all available skills (project / local / stdlib)
reyn skills <name>       # show usage details for one skill
```

### `reyn eval`

Run an eval spec against a target skill, scoring per-phase artifacts with an LLM judge:

```
reyn eval reyn/local/my_skill/eval.md
```

Eval specs are Markdown with frontmatter and per-phase quality criteria:

```markdown
---
type: eval
skill: reyn/local/my_skill/skill.md
dsl_root: reyn/local/
model: standard
---

## case: basic
input: "Write about AI in education for university students."

### phase: draft
quality:
- The article has a clear title
- The body is written for the specified audience
- [aspirational] The tone is engaging

### final
quality:
- The body is at least 500 characters
```

Aspirational criteria don't fail the case but are reported.

### `reyn lint`

```
reyn lint <skill_name>
```

Validates DSL structure: graph reachability, artifact references, transition targets, entry phase validity.

### `reyn events`

Replay a saved event log:

```
reyn events .reyn/runs/20260427T…_my_skill.jsonl
reyn events … --conversation                       # LLM context + responses
reyn events … --filter phase_started --skip llm_called
```

### `reyn config`

```
reyn config show                 # current effective config (merged sources)
reyn config fields               # all keys with descriptions and examples
reyn config get <key>
reyn config set <key> <value>    # writes to .reyn/config.yaml
```

### `reyn init`

```
reyn init                        # scaffold reyn.yaml + .reyn/config.yaml
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
                     ├── execute Control IR   (file / ask_user / shell / run_skill / lint)
                     ├── store artifact       (Workspace)
                     ├── emit Event           (JSONL log)
                     └── transition → next phase
```

Every state change is recorded as a structured event. Event logs support replay and offline debugging via `reyn events`.

---

## Project structure

```
src/
  reyn/                Runtime engine (OS, models, validation, compiler, CLI)
  stdlib/
    skills/            Bundled skills: skill_builder, skill_improver,
                       eval, eval_builder, judge_phase
    artifacts/         Shared artifacts available to every skill
    phases/            Shared phases available to every skill
reyn/
  local/<name>/        Your local skills (skill.md, phases/, artifacts/)
  project/<name>/      Skills checked into the project
dsl/
  skills/<name>/       Alternate location for skills (matched by --dsl-root)
  shared/              Shared artifacts and phases
.reyn/
  config.yaml          Per-project config (model, output_language, etc.)
  runs/                Event logs (one JSONL per run)
  artifacts/           Artifacts stored during runs
```

---

## Bundled skills

| Skill | Purpose |
|-------|---------|
| `skill_builder` | Generate a new skill from a natural-language description |
| `skill_improver` | Iteratively improve a skill against its eval until a score threshold is met |
| `eval` | Evaluate one case of a target skill using `judge_phase` |
| `eval_builder` | Auto-generate an `eval.md` from a target skill |
| `judge_phase` | LLM-as-judge over a single phase artifact and quality criteria |

Run `reyn skills <name>` for usage details.

---

## License

MIT — see [LICENSE](LICENSE).
