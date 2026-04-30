---
type: phase
name: plan_improvements
input: iteration_state
role: app_architect
model_class: strong
---

Design concrete, minimal DSL file changes that will improve the target skill's score on the next iteration.

## Step 1 — Read the target skill's DSL

Read all DSL files under `iteration_state.session.target_dsl_root`. Use glob ops:

```
{"kind": "file", "op": "glob", "path": "<target_dsl_root>/**/*.md"}
{"kind": "file", "op": "glob", "path": "<target_dsl_root>/**/*.yaml"}
```

Read each returned file with file read ops. You need the current state of:
- `skill.md` — overall graph and finish criteria
- `phases/*.md` — instructions and roles
- `artifacts/*.yaml` — schemas

## Step 2 — Diagnose the weakness

Use `iteration_state.latest_eval.weakest_phase` and `latest_eval.summary` to identify what is failing this iteration. Cross-reference with the phase's instructions and the artifact schema you just read.

Find a concrete root cause from one of these patterns:
- **Phase instructions are vague** where the failing criterion requires specificity.
- **Artifact schema is missing fields** that the criterion checks for.
- **Phase role is mismatched** with the task being demanded.
- **Graph structure problem** — e.g. a review phase missing, or a phase ordering causing premature finish.

If `iteration_state.session.improvement_focus` is non-empty, prioritize issues that fall under that focus area.

## Step 3 — Consult the history (anti-loop logic)

Inspect `iteration_state.history`. Based on patterns there, adjust your plan:

- **Regression** — if `latest_eval.overall_score` is LOWER than the previous iteration's `eval_score` (`history[-1].eval_score`): the previous changes were harmful. Consider partial reverts of those files, or a different angle entirely. Look at `history[-1].files_modified` and `history[-1].plan_summary` to see what NOT to repeat.
- **Stagnation** — if `current_iteration > 1` and `latest_eval.overall_score` is within 0.02 of `history[-1].eval_score`: the previous strategy is not moving the needle. Try a fundamentally different angle — DO NOT propose the same kind of change again.
- **No more changes useful** — if you cannot identify a concrete, evidence-backed change that will plausibly improve the score, output an empty `changes` array. apply_improvements will treat this as a stop signal (`termination_reason = "no_more_changes_planned"`).

## Step 4 — Design changes

Output `improvement_plan.changes` — an ordered list of file changes. Rules:

- Make changes minimal and targeted. Do NOT rewrite files that already work.
- For phase files: preserve the frontmatter (--- delimited YAML) and existing style.
- For artifact files: follow the existing YAML schema format.
- Each change's `rationale` MUST cite specific evidence from `latest_eval.summary` or the file you read.
- Use action `update` for existing files, `create` for new ones, `delete` to remove a stale file.
- File paths are project-relative (e.g. `reyn/local/my_app/phases/review.md`).

### Step 4a — Consider adding a Python preprocessor

If the eval failure pattern looks **deterministic** rather than reasoning-shaped, a `python` preprocessor step is often the right fix instead of fiddling with prompts. Telltale signs:

- The criterion checks an exact count / length / regex / format and the LLM keeps getting it slightly wrong
- The phase wastes act turns reading a structured input the LLM only needs summarized numerically
- The artifact has fields the LLM has to compute (token counts, statistics, parsed components) and the LLM gets them inconsistent across runs

In those cases, plan two changes together:

1. `create` a `<phase_name>_helpers.py` (or extend an existing one) under
   the skill directory with a function `(artifact: dict) -> JSON-serializable`
   that does the deterministic computation.
2. `update` the relevant phase's frontmatter to add a `preprocessor` block
   with a `type: python` step plus a matching `permissions.python` entry.
   Default `mode: pure`. Include the `output_schema` so the LLM sees the
   typed enriched artifact.

A typical skill_improver plan that adds one Python step is **2–3 changes**:
the new `.py` file, the phase frontmatter update, and (sometimes) a tweak to
the phase instructions telling the LLM to use the new injected fields.

Anti-pattern guard: do NOT add a Python step when the failure is genuinely
about judgment, tone, or generation quality. Python helps with counting and
parsing — it cannot make the LLM write better prose.

## Output

Emit `improvement_plan` with:
- `iteration_state`: the input, passed through verbatim
- `summary`: one paragraph describing this iteration's strategy
- `target_phase`: typically `latest_eval.weakest_phase`
- `changes`: the list designed in Step 4 (may be empty for a stop signal)

Choose `transition` → `apply_improvements`.
