---
type: tutorial
topic: getting-started
audience: [human]
---

# 02 — Your first skill

Build a working skill end-to-end using `skill_builder`. By the end you'll have a skill that takes a topic and writes a one-paragraph explainer.

## What you'll build

```
reyn run my_explainer "machine learning"
→ "Machine learning is a branch of ..."
```

A 2-phase skill:

1. `outline` — produce 3 bullet points
2. `expand` — turn the outline into a paragraph

## Step 1: Generate the skill scaffold

```bash
reyn run skill_builder "A skill that takes a topic and returns a one-paragraph explainer. Two phases: outline (3 bullets) then expand (paragraph)."
```

`skill_builder` is a stdlib skill. It plans the structure, designs artifacts, generates the phase markdown files, lints the result, and (optionally) revises if linting fails. The output lands in `reyn/local/my_explainer/` (the directory name is chosen during planning — you can rename it).

## Step 2: Inspect what it produced

```
reyn/local/my_explainer/
├── skill.md
├── phases/
│   ├── outline.md
│   └── expand.md
└── artifacts/
    ├── topic_input.yaml
    ├── outline.yaml
    └── explainer.yaml
```

Open `skill.md` — note the `graph:` and `final_output:` keys. Open `phases/outline.md` — note that it only declares `input:` and gives instructions, never an output schema.

This separation is core to reyn: see [concepts/principles.md](../../concepts/principles.md) for why.

## Step 3: Run it

```bash
reyn run my_explainer "photosynthesis"
```

Pass `--events` to see the underlying state transitions:

```bash
reyn run my_explainer "photosynthesis" --events
```

Each phase emits `phase_started`, `llm_called`, `artifact_created`, `phase_completed`. The full event log is replayable with `reyn events <log_file>`.

## Step 4: Iterate

If the output isn't what you wanted, use `skill_improver`:

```bash
reyn run skill_improver "my_explainer outputs are too academic — make them friendly and example-rich"
```

It reads your skill, plans changes, and proposes diffs. You approve before any file is written.

## What you learned

- **Skills are directories** of markdown + YAML, not Python code.
- **Phases declare input only** — outputs are determined by the next phase or the skill's `final_output`.
- **Building skills is itself a skill** — `skill_builder` and `skill_improver` are normal stdlib skills, not special tooling.

## Next

- [How-to: Write your first custom skill](../for-skill-authors/write-your-first-custom-skill.md) — build the same shape by hand, knowing what each file does.
- Tutorial 03 — Running a skill (CLI flags and inputs in depth) — Phase 2
- Tutorial 04 — Writing an eval — Phase 2
- [Reference: skill.md frontmatter](../../reference/dsl/skill-md.md)
