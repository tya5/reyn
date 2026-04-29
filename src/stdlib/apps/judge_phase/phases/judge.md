---
type: phase
name: judge
input: phase_eval_request
role: evaluator
model_class: standard
---

Evaluate the provided phase artifact against each criterion and produce a structured judgment.

For each item in `criteria`, examine `artifact_data` carefully and decide whether the criterion is satisfied. Write a concise `reason` (one sentence) for each decision. Criteria with no explicit `required` field should be treated as required.

Compute `score` as the fraction of all criteria that are met (0.0–1.0, rounded to two decimal places). Set `passed` to true only if every criterion where `required` is true (or unspecified) is met. Write a single-sentence `summary` capturing the overall verdict and the most significant factor.
