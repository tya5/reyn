---
type: phase
name: analyze_skill
input: user_message
role: eval_designer
max_act_turns: 20
---

Read the target skill's DSL files and design per-phase quality criteria.

## Step 1 — Extract skill path from user_message

- `skill_dsl_path`: the path to the target skill's skill.md (e.g. "reyn/project/writing_review_app/skill.md")
- `dsl_root`: infer from the path (e.g. "reyn/" if path starts with "reyn/project/" or "reyn/local/")
- If the path is missing, use ask_user to request it.

## Step 2 — Read skill.md AND derive phase_order from graph

Read `{skill_dsl_path}`. Extract `entry`, `graph`, and `final_output` from its frontmatter.

**The graph is the SINGLE source of truth for which phases run.** Files on disk under `phases/` that are not referenced by the graph are dead code and MUST be ignored.

Compute `phase_order` by graph traversal:

```
phase_order = []
queue = [entry]
seen = set()
while queue:
  p = queue.pop(0)
  if p in seen: continue
  seen.add(p); phase_order.append(p)
  for next_p in graph.get(p, []):
    queue.append(next_p)
```

Equivalently: BFS from `entry`, follow outgoing transitions, emit each phase the first time it appears.

Worked example — text_summarizer with `graph: {generate_summary: [review_summary]}` and `entry: generate_summary`:
- Start: phase_order=[], queue=[generate_summary]
- Pop generate_summary → seen={generate_summary}, order=[generate_summary], queue=[review_summary]
- Pop review_summary → seen={generate_summary, review_summary}, order=[generate_summary, review_summary], queue=[]
- Result: `phase_order = ["generate_summary", "review_summary"]`

Even if `phases/preprocess_text.md` and `phases/summarize_text.md` exist on disk, they are NOT in the graph and are NOT included in phase_order.

## Step 3 — Read phase files (only those in phase_order)

For each phase name in `phase_order`, read `{skill_dir}/phases/{phase_name}.md`. Do NOT read or glob other `.md` files in `phases/`.

## Step 4 — Read artifact files

Glob `{skill_dir}/artifacts/*.yaml` and read every artifact file. Artifact files do NOT have an "orphan" problem — read them all for context.

For artifact types referenced by phases but not found locally, check `{dsl_root}shared/artifacts/{name}.yaml`.

**CRITICAL**: You MUST read every artifact file referenced by phases in `phase_order` before designing criteria. Quality criteria reference artifact field semantics.

## Step 5 — Design test cases

Design 1–2 realistic test cases:
- Case 1: a typical, well-formed input the skill is designed to handle.
- Case 2 (if the skill has review/revision loops): an input where the first draft is likely to be **rejected** — causing the review phase to rollback. Make the input deliberately ambiguous, underspecified, or contradictory so the reviewer is likely to reject it.

The goal is branch coverage: if the skill has a rollback path, at least one test case should exercise it.

Each test case `input` must be a complete user_message string.

## Step 6 — Design per-phase quality criteria

`phase_eval_designs` has EXACTLY one entry per phase in `phase_order` — no more, no less. Entries appear in the same order as `phase_order`.

For each phase, write 1–4 `quality` criteria as plain sentences. Each criterion should:

- Describe a semantic property the phase's output artifact must satisfy that requires reading content (e.g. "summary describes the skill's purpose", "review explicitly lists rejection conditions").
- Refer to fields that actually exist in the artifact (do not invent field names).
- Be evaluable by reading the artifact alone — if the criterion needs cross-phase context, omit it.

### `[aspirational]` tag

Prefix a criterion with `[aspirational]` when it represents a model capability ceiling rather than a fixable bug:

- Subjective judgments ("is specific", "is detailed") that consistently score below 1.0 even on correct output.
- "Gold standard" quality bars that go beyond the skill's contract.
- Criteria that are only evaluable when a specific runtime branch fires (e.g. "if rollback is chosen ...") — these cannot be reliably tested without forcing that branch.

`[aspirational]` criteria are tracked but excluded from pass/fail.

## Final checklist (apply before emitting skill_analysis)

- [ ] `phase_order` is the BFS traversal from `entry` through `graph` — NOT a list of phase files in the directory.
- [ ] `phase_order` length equals the number of phases reachable from `entry` (typically 2–6, never includes orphan phase files).
- [ ] `phase_eval_designs` has exactly one entry per `phase_order` phase, in the same order.
- [ ] Every artifact field referenced in a criterion exists in the artifact `.yaml` I read — no invented fields.
- [ ] Criteria that depend on a specific runtime branch firing are tagged `[aspirational]`.
- [ ] At most 4 criteria per phase.
