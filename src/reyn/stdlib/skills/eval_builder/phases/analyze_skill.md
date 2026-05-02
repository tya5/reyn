---
type: phase
name: analyze_skill
input: user_message
role: eval_designer
max_act_turns: 7
allowed_ops: [file]
---

Read the target skill's DSL files and design per-phase quality criteria.

## Step 1 — Extract skill path from user_message

- `skill_dsl_path`: the path to the target skill's skill.md (e.g. "reyn/project/writing_review_app/skill.md")
- `dsl_root`: infer from the path (e.g. "reyn/" if path starts with "reyn/project/" or "reyn/local/")
- If the path is missing or cannot be inferred, emit `control.type="abort"` with a reason explaining what was missing. Do NOT use ask_user — eval environments are non-interactive and ask_user will always fail with EOF.

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

While reading each phase, **note whether it has a `preprocessor` block**, and if any step has `type: python`, record:

- the python step's `into` path (e.g. `data.stats`)
- the names of the fields it injects (read its `output_schema.properties`)
- the function `module:function` (for the criterion to reference precisely)

This information shapes the criteria you write in Step 6 — see the
"Phases with python preprocessor" subsection.

## Step 3.5 — Read existing eval.md if present (preserves user-curated cases)

Before designing new cases, attempt `file.read({skill_dir}/eval.md)`. If the file
is found, parse its `## case:` blocks to extract the existing case names AND
their `input:` strings — these are the canonical case identities. Any new
criteria you write MUST reuse those `case_name` values verbatim. Do not rename
existing cases (`narrator output conciseness` ≠ `typical_finished_skill`) — the
downstream eval/improver loop joins on case_name.

If the file is not found (`[denied]`, `not_found`, or any error), proceed with
fresh case design grounded in `phase_order` from Step 2.

When extending an existing eval.md, you MAY add NEW cases for branches not yet
covered, but the existing case names stay as they are. Phase names referenced
in any case's `phase_criteria` MUST be members of `phase_order` — never invent
phase names like `improve_skill` if the graph contains only `narrate`.

## Step 4 — Read artifact files

Issue a glob op for `{skill_dir}/artifacts/*.yaml` using the `path` field:
`{"kind": "file", "op": "glob", "path": "{skill_dir}/artifacts/*.yaml"}`

Then read every file returned. Artifact files do NOT have an "orphan" problem — read them all for context.

For artifact types referenced by phases but not found locally, check `{dsl_root}shared/artifacts/{name}.yaml`.

**CRITICAL**: You MUST read every artifact file referenced by phases in `phase_order` before designing criteria. Quality criteria reference artifact field semantics.

## Step 5 — Design test cases WITH per-case criteria

Design 2–3 test cases. For each case, design its `phase_criteria` at the same time — criteria must reflect what THAT specific case is testing, not generic criteria copied across all cases.

**Case types to cover:**
- Case 1 (always): typical, well-formed input the skill is designed to handle.
- Case 2 (if the skill has review/revision loops): an input where the first draft is likely to be **rejected** — make it deliberately ambiguous, underspecified, or contradictory. Criteria should test that the review phase actually rejects (e.g. "the review verdict is 'reject' and cites a specific flaw").
- Case 3 (if any phase has a python preprocessor): an **edge case for the python step** — empty, minimal, or unusually large input. Criteria should test that the LLM handles boundary values from the preprocessor (e.g. "when char_count=0, the commentary acknowledges the empty input rather than fabricating statistics").

The goal is branch coverage: if the skill has a rollback path, at least one case should exercise it.

Each test case `input` must be a **plain text string** — the raw message the user would type. Do NOT wrap it in `{"type":"user_message",...}` JSON. Write just the text, e.g. `"Hello world"`, not `"{\"type\":\"user_message\",\"data\":{\"text\":\"Hello world\"}}"`.


### Criteria rules (apply per case)

For each case's `phase_criteria`, write 1–4 `quality` criteria per phase as plain sentences. Each criterion must:

- Describe a semantic property **observable for this specific input** — not a generic property true for all inputs.
- Refer to fields that actually exist in the artifact (do not invent field names).
- Be evaluable by reading the artifact alone.

**`[aspirational]` tag**: prefix when the criterion represents a model capability ceiling (subjective judgments, gold-standard bars, or branch-dependent checks). These are tracked but excluded from pass/fail.

### Phases with a python preprocessor

If a phase has a `type: python` preprocessor step, the python computation is deterministic. Write criteria that test the LLM's **integration** with the python output. Reference **numeric values**, not internal field paths:

- DO: "The commentary cites the exact character count computed by the preprocessor (e.g. states '156 characters')."
- DO: "For empty input, the commentary acknowledges that there is no text rather than fabricating statistics."
- DO (case-specific): "The commentary reports exactly 2 characters and 1 word." (for a `"hi"` test case)
- DON'T: ~~"The commentary cites `data.stats.char_count` verbatim."~~ — The commentary should cite the NUMBER, not the Python field path. Users see values, not field names.
- DON'T: ~~"`char_count` is the correct number."~~ — Python guarantees this; useless signal.

## Final checklist (apply before emitting skill_analysis)

- [ ] `phase_order` is the BFS traversal from `entry` through `graph` — NOT a list of phase files in the directory.
- [ ] Every `phase_criteria[*].phase` value is a member of `phase_order`. No invented phase names (e.g. if the graph only has `narrate`, `phase` MUST be `narrate` — never `improve_skill`, `revise`, etc.).
- [ ] If an existing eval.md was found in Step 3.5, every test case I emit either reuses one of its case names verbatim or is a clearly-marked NEW case that doesn't collide with existing ones.
- [ ] Every test case has a `phase_criteria` array with at least one phase entry.
- [ ] Criteria differ meaningfully between cases — no verbatim copies from one case to another.
- [ ] Every artifact field referenced in a criterion exists in the artifact `.yaml` I read — no invented fields.
- [ ] Criteria that depend on a specific runtime branch firing are tagged `[aspirational]`.
- [ ] At most 4 criteria per phase per case.
