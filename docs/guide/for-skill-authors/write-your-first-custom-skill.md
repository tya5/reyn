---
type: how-to
topic: skill-authoring
audience: [human]
applies_to: [reyn/local/, reyn/project/]
---

# Write your first custom skill

**Goal:** Build a skill from scratch — by hand, knowing what each file does — instead of letting `skill_builder` generate one.

## When to use this how-to

- You followed [Tutorial 03 — Your first skill](../getting-started/03-your-first-skill.md) and now want to understand the pieces directly.
- You want a degree of control `skill_builder` doesn't give (specific phase names, an unusual graph shape, hand-tuned instructions).
- You're porting a workflow from another framework where the structure is already decided.

If you just want a skill that works, run `skill_builder` first and edit its output — that's almost always faster.

## Prerequisites

- Reyn installed and running (`reyn run direct_llm "hi"` should answer).
- Read [Concepts: phase vs skill vs OS](../../concepts/phase-vs-skill-vs-os.md) — the contract this how-to follows.

## The skill directory layout

Every skill is a directory with three things:

```
reyn/local/my_skill/
├── skill.md            # frontmatter: graph + final_output + permissions
├── phases/             # one .md per phase
│   ├── <entry>.md
│   └── <next>.md
└── artifacts/          # one .yaml per artifact type
    └── <name>.yaml
```

No Python is required. Skills are plain text.

## What we'll build

A 2-phase skill that takes a user message and returns a short reaction grounded in a Python-computed character count. We'll call it `react_to_text`.

```
user message → count_chars (python preprocessor) → react (LLM) → reaction
```

This is intentionally small: it exercises Skill, Phase, Artifact, the Python preprocessor, and the `final_output` contract — every concept you need for real skills.

## Step 1: Create the directory

```bash
mkdir -p reyn/local/react_to_text/{phases,artifacts}
```

## Step 2: Define the artifact

`artifacts/reaction.yaml`:

```yaml
type: object
required: [comment, char_count]
properties:
  comment:
    type: string
    description: One-sentence reaction to the user's text.
  char_count:
    type: integer
    minimum: 0
    description: Exact character count of the input.
```

Artifacts are JSON Schema in YAML. The filename (`reaction.yaml`) is the artifact **type name** (`reaction`). Other phases and the skill's `final_output` reference it by that name.

You don't need an artifact for the user's input — the stdlib provides `user_message` (a `{text: string}` shape).

## Step 3: Write a Python preprocessor

`stats.py`:

```python
def count_chars(payload):
    """payload is the artifact dict; we read user text from it."""
    text = payload["data"]["text"]
    return {"char_count": len(text)}
```

Pure functions only — no I/O, no globals. The runtime sandboxes them.

## Step 4: Write the phase

`phases/react.md`:

```yaml
---
type: phase
name: react
input: user_message
role: reactor
can_finish: true
allowed_ops: []
preprocessor:
  - type: python
    module: ./stats.py
    function: count_chars
    into: data.stats
    output_schema:
      type: object
      required: [char_count]
      properties:
        char_count: {type: integer, minimum: 0}
---

Write a one-sentence reaction to the user's text. Reference the exact
character count.

## Inputs

- `input_artifact.data.text` — what the user said.
- `input_artifact.data.stats.char_count` — precomputed by Python. Quote
  this number verbatim; don't re-count.

## Style

Match the user's language. Stay under 25 words. No meta-commentary.
```

Notice what's **not** in the phase frontmatter:

- No `output:` field. The output shape is determined by the skill's `final_output`, not the phase. ([P1](../../concepts/principles.md#p1-phase-is-stateless-and-reusable))
- No `next_phase:`. The skill graph owns transitions.
- No artifact field list in instructions. The OS injects the schema at runtime. ([P8](../../concepts/principles.md#p8-phase-instructions-contain-only-domain-logic))

## Step 5: Write the skill

`skill.md`:

```yaml
---
type: skill
name: react_to_text
description: Take a user message and return a short reaction with exact char count.
entry: react
final_output: reaction
final_output_description: |
  One-sentence comment plus the exact char count of the input.
finish_criteria:
  - The comment references the actual character count
graph:
  react: []
permissions:
  python:
    - module: ./stats.py
      function: count_chars
      mode: pure
      timeout: 5
---

## What this skill does

Counts the characters in a user message deterministically (Python), then
asks the LLM to produce a short reaction grounded in that number.
```

`graph: { react: [] }` means the skill has one phase and ends after it. `[]` is the terminal marker.

`permissions:` declares every capability any phase in the skill needs. The runtime audits this at startup. ([Skill-only permissions](../../reference/dsl/skill-md.md#permissions-skill-level))

## Step 6: Lint

```bash
reyn lint react_to_text
```

The linter checks the contract: required fields, graph reachability, artifact references, permission declarations. Fix any errors before running — they catch most P1/P8 mistakes.

## Step 7: Run

```bash
reyn run react_to_text "Hello, this is a test."
```

Expected output: a one-line reaction that names `22` (the actual char count) verbatim.

Pass `--events` to see what the OS did:

```bash
reyn run react_to_text "Hello, this is a test." --events
```

You'll see `phase_started` → `preprocessor_step_completed` (the Python step) → `llm_called` → `artifact_created` → `phase_completed` → `skill_completed`.

## Mental model

Three contracts to keep straight:

| File | Owns | Doesn't own |
|------|------|-------------|
| `skill.md` | graph, final output, permissions | what each phase does |
| `phases/<name>.md` | input + instructions | output shape, next phase |
| `artifacts/<name>.yaml` | data schema | who writes/reads it |

If you find yourself naming the next phase from a phase, or listing output fields in phase instructions, you've crossed a boundary. The OS will flag the easy mistakes; the subtle ones make skills brittle.

## Common mistakes

- **Listing output fields in phase instructions.** The OS already injects the schema. Re-stating it produces drift. [P8](../../concepts/principles.md#p8-phase-instructions-contain-only-domain-logic)
- **Telling the phase which phase is next.** Phases don't know. The skill graph + LLM decision pick. [P1](../../concepts/principles.md#p1-phase-is-stateless-and-reusable)
- **Skipping `final_output`.** Required. The OS validates the final artifact against this schema; without it there's no contract for callers.
- **Putting permissions on the phase.** Permissions live on the skill since the [skill-only-permissions migration](../../reference/dsl/skill-md.md#permissions-skill-level). Phase frontmatter rejects them.
- **Recomputing in the LLM what Python already gave you.** The whole point of the preprocessor is to remove that responsibility — keep it removed. ([Concept: deterministic split](../../concepts/agent-engineering/system-design.md))

## Real examples to copy from

The smallest stdlib skills are good starting templates:

- [`word_stats_demo`](https://github.com/tya5/reyn/tree/main/src/reyn/stdlib/skills/word_stats_demo) — single phase + Python preprocessor (this how-to mirrors its shape).
- [`direct_llm`](https://github.com/tya5/reyn/tree/main/src/reyn/stdlib/skills/direct_llm) — single phase, no preprocessor, plain user_message → text.
- [`read_local_files`](https://github.com/tya5/reyn/tree/main/src/reyn/stdlib/skills/read_local_files) — two phases, MCP op, the canonical multi-phase example.

## Promotion to `reyn/project/`

Once `reyn lint` is clean and you have at least one happy-path eval case (see [Tutorial 05](../getting-started/05-writing-an-eval.md)), move the directory:

```bash
git mv reyn/local/react_to_text reyn/project/react_to_text
```

`reyn/local/` is for in-progress work; `reyn/project/` is checked-in skills. The lookup order is `project → local → stdlib`. ([CLAUDE.md skill resolution](https://github.com/tya5/reyn/blob/main/CLAUDE.md))

## Next

- [Compose skills with `run_skill`](compose-skills-with-run-skill.md) — call one skill from another.
- [Validate artifacts](validate-artifacts.md) — strict-mode checks and schema patterns.
- [Add a Python preprocessor](add-a-python-preprocessor.md) — `pure` vs `trusted` modes, deeper signatures.
- [Reference: `skill.md` frontmatter](../../reference/dsl/skill-md.md)
- [Reference: `phase.md` frontmatter](../../reference/dsl/phase-md.md)
- [Reference: `artifact.yaml`](../../reference/dsl/artifact-yaml.md)
