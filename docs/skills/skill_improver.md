# skill_improver — Iteratively improve an existing skill

Runs the target skill under an eval, plans concrete DSL changes against the failing criteria, applies them, and re-evaluates — repeating until a score threshold is met or a stop condition fires.

---

## What it does

- Scores the target skill against an `eval.md` spec (auto-generates one via [eval_builder](eval_builder.md) if missing)
- Diagnoses the weakest phase from the failing criteria
- Proposes minimal DSL changes to phase instructions and artifact schemas
- Writes the patches in place and re-runs the eval — the OS rollback drives the loop
- Stops when the score threshold is met, max iterations is reached, or quality regresses

---

## Usage

```bash
reyn run skill_improver "Improve reyn/local/my_skill, max 3 iterations, threshold 0.9"
```

Or pass a structured input directly:

```bash
reyn run skill_improver '{
  "type": "improvement_request",
  "data": {
    "target_skill_path": "reyn/local/my_skill/skill.md",
    "max_iterations": 3,
    "score_threshold": 0.9
  }
}'
```

> **Note:** `skill_improver` invokes the `eval` and `eval_builder` skills via the `run_skill` Control IR op. No `--allow-shell` flag is required.

---

## Input format

Provide the following information in natural language or as a structured artifact:

| Field | Required | Description |
|-------|----------|-------------|
| Target skill path | yes | Path to the target skill's `skill.md` (e.g. `reyn/local/my_skill/skill.md`) |
| Eval spec path | optional | Path to an existing `eval.md`. If omitted, `skill_improver` runs `eval_builder` to create one next to the target |
| Max iterations | optional | Cap on improvement loop iterations (default: 3) |
| Score threshold | optional | Stop early once this score is reached (default: 0.9) |

If `target_skill_path` is missing, the `prepare` phase asks for it via `ask_user`.

---

## Phase flow

```
prepare  →  run_and_eval  →  plan_improvements  →  apply_improvements
                  ↑___________________________________|
                       (rollback for next iteration)
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `prepare` | coordinator | Parses the request, ensures an `eval.md` exists (auto-generates via `eval_builder` if not), picks a test case, initializes session state |
| `run_and_eval` | evaluator | Invokes the `eval` stdlib skill via `run_skill`; records the score in `iteration_state` |
| `plan_improvements` | architect | Reads the target's DSL files, diagnoses the weakest phase, and proposes minimal DSL changes targeting failing criteria. Adapts strategy from iteration history (regression / stagnation detection) |
| `apply_improvements` | implementer | Writes the proposed changes to disk, persists iteration state to `improver_state.json`, then either **finishes** or **rolls back** to `run_and_eval` for iteration N+1 |

The loop is realized via OS rollback. When `apply_improvements` rolls back, the next visit to `run_and_eval` reads the just-modified DSL on disk and starts a fresh iteration.

---

## Output

Improvements are applied **in place** to the target skill's DSL files. There is no separate `dsl_patches/` directory — `apply_improvements` writes directly to the canonical paths each iteration.

Final output:

```json
{
  "type": "improvement_result",
  "data": {
    "target_skill_path": "reyn/local/my_skill/skill.md",
    "iterations_performed": 3,
    "initial_score": 0.55,
    "final_score": 0.92,
    "score_history": [0.55, 0.78, 0.92],
    "files_modified": [
      "reyn/local/my_skill/phases/review.md",
      "reyn/local/my_skill/artifacts/review_verdict.yaml"
    ],
    "termination_reason": "score_threshold_met",
    "summary": "Clarified review-phase instructions and added explicit verdict-field semantics; score climbed 0.55 → 0.92 over 3 iterations.",
    "next_steps": "reyn eval reyn/local/my_skill/eval.md"
  }
}
```

`termination_reason` is one of:

| Value | Meaning |
|-------|---------|
| `score_threshold_met` | Reached the configured threshold |
| `max_iterations_reached` | Hit the iteration cap |
| `regression_detected` | Score went down — change rolled back, loop stops |
| `stagnation_detected` | Two consecutive iterations with no score change |
| `no_more_changes_planned` | `plan_improvements` could not find a productive next change |

---

## What plan_improvements looks at

The improver prioritizes the **weakest phase from the latest eval** and proposes targeted, minimal patches:

| Signal | What it means |
|--------|---------------|
| Failing required criteria | Highest priority — direct quality target |
| Aspirational criteria | Lower priority; nudged but never blocks scoring |
| Per-phase score breakdown | Identifies which phase to focus on next |
| Iteration history | Detects regression (score drop) and stagnation (no change) |
| Prior change types | Avoids repeating the same kind of change that didn't help |

---

## Tips

- **Build the eval first**: a well-targeted [eval_builder](eval_builder.md) spec gives the improver clear failure signals to optimize against
- **Threshold + max_iterations are guard rails**: defaults are conservative — raise `max_iterations` if you want longer runs, lower `score_threshold` if 0.9 is unreachable for the task
- **Regression rollback is automatic**: if iteration N+1 scores lower than N, the change is reverted and the loop exits with `regression_detected` — your DSL is left in the best-scoring state
- **Inspect `improver_state.json`** in the project root for full iteration history if you want to see exactly what changed each round
