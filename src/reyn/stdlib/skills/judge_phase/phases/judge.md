---
type: phase
name: judge
input: phase_eval_request
role: evaluator
model_class: standard
allowed_ops: [read_file, write_file, edit_file, delete_file, glob_files, grep_files]
---

Evaluate the provided phase artifact against each criterion and produce a structured judgment.

## Step 1 — Read the artifact

Issue a file read op for `artifact_path`. The file contains a JSON object; extract its `.data` field — that is the artifact data to evaluate.

## Step 2 — Judge each criterion

For each item in `criteria`, examine the artifact data carefully and decide whether the criterion is satisfied. Mark `met` as true only when the artifact clearly demonstrates the criterion. Write a concise `reason` (one sentence) for each decision. Criteria with no explicit `required` field should be treated as required.

Focus your judgment on the substance — the quality of each `met` / `reason` pair is what matters. Do not compute or output any aggregate numeric score; that is derived deterministically downstream.

## Step 3 — Produce judgment

Set `passed` to true only if every criterion where `required` is true (or unspecified) is met. Write a single-sentence `summary` capturing the overall verdict and the most significant factor.
