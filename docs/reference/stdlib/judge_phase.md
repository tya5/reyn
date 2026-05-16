---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [judge_phase]
---

# `judge_phase`

Evaluate a single phase artifact against quality criteria and return a structured judgment with a deterministic score.

## Entry

`judge`

## Final output

`phase_judgment` — per-criterion `met`/`reason` pairs, overall `passed` boolean, one-sentence `summary`, and numeric `score` (= passed criteria / total criteria) added by the postprocessor.

## How it composes

Single-phase skill. The `judge` phase reads the artifact under evaluation via a `file` op, scores each criterion from the input `criteria` list (`met` + `reason`), sets `passed` (true only if all required criteria are met), and writes a `summary`. The LLM produces `phase_judgment_raw` (no `score`); the postprocessor computes `score = passed/total` in pure Python and emits the caller-facing `phase_judgment`. This isolates arithmetic from the LLM contract.

## Caveats

- Designed as a sub-skill for use inside `eval` skill preprocessors via `iterate × run_skill(judge_phase)` — not intended for direct standalone invocation.
- The `judge` phase has `allowed_ops: [file]` — a file read op is required to load the artifact data.
- `score` is computed deterministically; the LLM must not attempt to calculate it.
- `graph: {}` — no outbound transitions; the skill always finishes after one phase.

## Usage

Invoke via `run_skill` in an eval skill's preprocessor, not from the CLI. The input artifact is `phase_eval_request` containing an `artifact_path` and a `criteria` list.

```yaml
# Inside an eval skill preprocessor:
- op: run_skill
  skill: judge_phase
  input: phase_eval_request
```

## Source

[`src/reyn/stdlib/skills/judge_phase/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/judge_phase/skill.md)
