---
type: tutorial
topic: getting-started
audience: [human]
---

# 04 — Writing an eval

Evals turn "the output looked good" into "the output passed N criteria across M cases." This tutorial covers building a rubric for `my_explainer` (the skill from tutorial 02) and running it.

## The shape of an eval

An eval spec is a markdown file with frontmatter and one or more cases:

```markdown
---
skill_dsl_path: my_explainer
model: standard
---

# Case: short_topic

input: photosynthesis

## Phase: outline
- Each bullet is a complete sentence.
- Bullets cover distinct angles (no overlap).

## Phase: expand
- The paragraph mentions all three bullet points.
- The tone is friendly, not academic.
```

Each `## Phase: <name>` block lists rubric criteria. The `eval` skill judges each criterion phase-by-phase using `judge_phase`.

## Step 1: generate a draft

```bash
reyn run eval_builder "build an eval for my_explainer covering tone and structure"
```

`eval_builder` reads the skill, drafts cases and criteria, and writes `reyn/local/my_explainer/eval.md`.

## Step 2: review it

Open the file. Adjust:

- **Cases** — add edge cases (empty topic, very long topic, ambiguous topic).
- **Criteria** — sharpen vague ones into testable statements. "The paragraph is well-written" doesn't grade reliably; "the paragraph is 2-4 sentences" does.

## Step 3: run it

```bash
reyn eval reyn/local/my_explainer/eval.md
```

Output:

```
=== Eval: my_explainer  [3 case(s)] ===
    model=standard

━━━ case: short_topic ━━━
  input: photosynthesis
  ✓ score=0.95  (4/4 required)

━━━ case: long_topic ━━━
  ...

═══════════════════════════════════════════════════
 ✓ 3/3 cases passed
 Results → .reyn/eval_reports/my_explainer/<timestamp>.json
═══════════════════════════════════════════════════
```

`reyn eval` exits with status 0 (all passed), 1 (spec failed to load), or 2 (cases failed).

## Step 4: tighten the rubric

If a criterion is "passing on bad output," it's not specific enough. Look at the failing case:

```bash
cat .reyn/eval_reports/my_explainer/<timestamp>.json
```

For each failed criterion, the report includes the judge's reasoning. Use it to rewrite the criterion to be more specific, then re-run.

## Eval is non-interactive

`reyn eval` doesn't prompt. Any permission the target skill needs must be pre-approved (in `reyn.yaml`'s `permissions:` or saved to `.reyn/approvals.yaml` from a prior `reyn run`). Without it, the case is reported as not-finished. See [manage-permissions](../how-to/manage-permissions.md).

## What you learned

- An eval spec is a phase-keyed rubric in markdown.
- `eval_builder` generates a draft; you review and iterate.
- `reyn eval` runs every case non-interactively and writes a report.

## Next

- [Tutorial 05 — Chat mode](05-chat-mode.md)
- [Reference: stdlib/eval](../reference/stdlib/eval.md)
- [Reference: stdlib/eval_builder](../reference/stdlib/eval_builder.md)
