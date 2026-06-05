---
type: skill
name: swe_bench
description: Solve a SWE-bench task — reproduce a GitHub issue fix in a real repository, verify with the provided tests, and emit a git diff patch.
entry: setup
final_output: swe_bench_result
final_output_description: |
  The git diff patch produced for this SWE-bench instance, whether the tests
  passed, and how many apply/verify attempts were made.
finish_criteria:
  - setup has checked out base_commit and prepared the environment
  - explore has identified the relevant code regions
  - plan has recorded a concrete edit plan in the workspace
  - apply has executed all planned edits
  - verify has run the test_patch tests and recorded the outcome
  - report has captured the final git diff as the patch string
  - swe_bench_result records instance_id, patch, tests_passed, and attempts
graph:
  setup:   [explore]
  explore: [plan]
  plan:    [apply]
  apply:   [verify]
  verify:  [report, plan]
  report:  []
routing:
  intents: [task]
  when_to_use:
    - Input contains instance_id, base_commit, and problem_statement (SWE-bench format)
    - User wants to run a SWE-bench task or GitHub issue fix benchmark
    - Batch evaluation loop calls this skill per SWE-bench instance
  when_not_to_use:
    - User wants to fix arbitrary code without a base_commit anchor
    - No test_patch provided to verify the fix (use skill_builder or direct_llm instead)
    - User wants to evaluate an existing skill's quality (use eval skill instead)
permissions:
  file.read:
    - path: "*"
      scope: recursive
  file.write:
    - path: "*"
      scope: recursive
  # FP-0008 #1115 Stage 2: all phases migrated off the deprecated `shell` op to
  # `sandboxed_exec` (which has default permissibility — no permissions entry).
  # `permissions.shell` removed: no phase emits `kind: shell` any more.
  # FP-0008 PR-O v8: deterministic test_patch sanitizer (= line-ending
  # / BOM / trailing-newline normalization) runs as a `safe`-mode
  # python preprocessor step at the verify phase entry, BEFORE the
  # LLM issues `git apply`. Eliminates the sandbox_2 v8 test_patch
  # apply-reject failure class structurally.
  python:
    - module: ./sanitize_test_patch.py
      function: sanitize_test_patch
      mode: safe
      timeout: 5
    # FP-0008 C6 v2: pure string parser — extracts +++ b/<path> targets from
    # test_patch and returns git checkout argv lists.  Mode: safe because
    # the function uses only re + json (both in PURE_STDLIB_ALLOWLIST) and
    # performs no filesystem access, subprocess calls, or environment reads.
    - module: ./parse_test_targets.py
      function: parse_test_targets
      mode: safe
      timeout: 5
    # #1209 PR-B: regex-escapes each plan edit's verbatim `anchor` so the apply
    # preprocessor's grep matches it literally.  Mode: safe — uses only re
    # (PURE_STDLIB_ALLOWLIST), pure data transform, no filesystem/subprocess/env.
    - module: ./escape_anchors.py
      function: escape_anchors
      mode: safe
      timeout: 5
    # #1216: deterministically drops not-locatable edits (region count 0) from
    # the actionable plan + records them in `not_locatable`, post the iterate-grep.
    # Mode: safe — pure data transform, no filesystem/subprocess/env.
    - module: ./drop_not_locatable.py
      function: drop_not_locatable
      mode: safe
      timeout: 5
    # #1366: extracts problem_statement code-symbols (NOT test_patch — avoids
    # deepening leakage) and pairs them with the explore relevant_files, so the
    # plan preprocessor's iterate-grep can place the problem-relevant regions
    # into context before the plan LLM (plan-layer analogue of #1209). Mode: safe
    # — uses only re + json (PURE_STDLIB_ALLOWLIST), pure data transform.
    - module: ./extract_problem_symbols.py
      function: extract_problem_symbols
      mode: safe
      timeout: 5
# FP-0016 D: this skill needs no static secrets / OAuth tokens.
required_credentials: []
---

## Overview

Solves a single SWE-bench Verified task end-to-end: checks out the target
commit, explores the codebase to understand the problem, plans and applies
edits, verifies against the provided test patch, then emits a `git diff`
patch as the final output.

All required capabilities (`read_file`, `edit_file`, `sandboxed_exec`, `grep`)
are existing OS ops — no OS changes required (P7 compliant).

## Phase flow

```
setup → explore → plan → apply → verify → report
```

`verify` always proceeds to `report`, carrying the verdict (`tests_passed` +
`failure_summary`); `report` produces the best-effort patch whether the tests
passed or failed. A test-failure re-plan loop (verify → plan to revise the fix)
is a tracked enhancement, not yet wired into the graph.

| Phase   | Role        | Responsibility |
|---------|-------------|----------------|
| setup   | initializer | Check out base_commit, confirm test runner is available |
| explore | analyst     | Grep and read to find relevant code; save exploration notes |
| plan    | architect   | Decide which files to edit and what to change |
| apply   | implementer | Execute the plan via file edits |
| verify  | tester      | Run test_patch tests; record the `tests_passed` verdict, then report |
| report  | reporter    | Run `git diff HEAD` to produce the final patch |

## Input

Structured `swe_bench_input` artifact matching the SWE-bench dataset format:

```json shape_only
{
  "type": "swe_bench_input",
  "data": {
    "instance_id": "django__django-12345",
    "repo": "django/django",
    "base_commit": "abc123def456",
    "problem_statement": "BUG: ...",
    "hints_text": "Look at foo/bar.py ...",
    "test_patch": "diff --git a/tests/... ..."
  }
}
```

## Output

`swe_bench_result` with the git diff patch, pass/fail outcome, and attempt count.
