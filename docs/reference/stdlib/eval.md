---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [eval]
---

# `eval`

Evaluate a target skill against a single test case using `judge_phase` as LLM-as-judge.

## Entry

`run_target`

## Final output

`eval_result` — overall pass/fail, score, per-criterion summary, weakest phase.

## How it composes

The `evaluate` phase uses an `iterate × run_skill` preprocessor that fans `judge_phase` out over per-criterion eval requests. The LLM only aggregates the per-criterion judgments — the iteration itself is deterministic OS code.

## Caveats — Python preprocessor approval

If the target skill uses Python preprocessor steps, **each step must be approved before eval**. eval invokes the target via `run_skill` under a non-interactive permission resolver — there is no prompt at eval time.

Two ways to pre-approve:

1. Run the target once interactively first (`reyn run <target> "<sample>"`); approval is saved to `.reyn/approvals.yaml`.
2. Set a project-wide allow in `reyn.yaml`:

   ```yaml
   permissions:
     python:
       pure: allow
       trusted: allow   # also requires --allow-untrusted-python
   ```

Without prior approval, the target's run fails and the case is reported as not-finished.

## Usage

`eval` is normally invoked indirectly via `reyn eval <spec.md>`, which iterates over multiple cases and aggregates results. CLI reference at `reference/cli/eval.md` (Phase 2).

## Source

[`src/stdlib/skills/eval/skill.md`](https://github.com/tya5/reyn/blob/main/src/stdlib/skills/eval/skill.md)
