---
type: agent
topic: stdlib
audience: [agent]
applies_to: [skill_importer]
---

# `skill_importer` — mapping rules

Use this when you (the `skill_importer` skill) translate an external prompt or workflow into a reyn skill. The job is **one direction only**: source → reyn DSL. The output should pass `reyn lint` cleanly.

## Source patterns and their reyn mappings

### Single prompt → single phase

```
Source:  "Summarize the input in 3 bullets, then expand into a paragraph."
Reyn:    Two phases: outline (3 bullets) → expand (paragraph). Skill graph
         outline:[expand], expand:[end].
```

If the source is genuinely a single transformation, use one phase + `entry → end`. If it has obvious sub-steps, split.

### Linear sub-steps → linear graph

```
Source:  "Step 1: extract entities. Step 2: classify each entity. Step 3: format the report."
Reyn:    Three phases (extract, classify, format). Graph:
           extract: [classify]
           classify: [format]
           format: [end]
```

### Conditional branches → branching graph

```
Source:  "If the input is a question, answer it. Otherwise, summarize."
Reyn:    triage phase with graph triage: [answer, summarize]
         answer: [end]
         summarize: [end]
```

The branch decision is the LLM picking among candidate transitions in the triage phase. Don't encode the condition in phase instructions as control flow ("if X, set next_phase to Y"); the LLM picks among `candidate_outputs` directly.

### Loops → review/revise pattern

```
Source:  "Draft the answer, then revise until it satisfies the criteria."
Reyn:    draft → review → [revise, end]; revise → review.
```

Self-loops aren't supported. Always go through a separate phase (`revise`).

### Tool calls → Control IR

| Source pattern | Reyn equivalent |
|----------------|-----------------|
| "Read file X" | `file.read` Control IR op |
| "Search the web for X" | MCP search tool (declared in skill permissions) |
| "Run command X" | `shell` Control IR op (requires `--allow-shell`) |
| "Ask the user about X" | `ask_user` Control IR op |
| "Look up project memory" | preprocessor `run_skill: recall_memory` |

Don't translate every "look up X" into `recall_memory`. Only when the source clearly intends to read user/project state.

### Repeated outputs → artifact schema

```
Source:  "Return JSON like {entities: [{name, type, confidence}, ...]}"
Reyn:    artifacts/entity_list.yaml with that schema. Phase output flows into
         this shape via P1 (next-phase input or final_output_schema).
```

## Don't do

- **Don't infer fields not in the source.** If the source doesn't ask for `quality_score`, don't add it.
- **Don't invent decision values.** Use `continue`/`finish`/`abort` only. No `revise`, `redo`. [P7]
- **Don't enumerate output fields in phase instructions.** [P8]
- **Don't put control flow in phase instructions.** Encode it in the graph. [P1]
- **Don't add Python preprocessor steps the source didn't suggest.** Keep imports minimal.

## Output of an import

The importer should produce:

1. `reyn/local/<name>/skill.md`
2. `reyn/local/<name>/phases/*.md`
3. `reyn/local/<name>/artifacts/*.yaml`
4. (Optional) `reyn/local/<name>/<module>.py` for Python preprocessor steps.

Then run `reyn lint <name>` via the `lint` Control IR op. If linting fails, revise once. If it still fails, surface the issues to the user — don't keep iterating.

## Promotion guidance (for the user)

After import, the user should:

1. Review the generated structure manually.
2. Write a sample input and `reyn run <name>` to verify behavior.
3. Run `skill_improver` to refine if needed.
4. Move from `reyn/local/` to `reyn/project/` when ready to commit.
