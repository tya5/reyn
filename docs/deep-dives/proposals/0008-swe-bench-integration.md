# FP-0008: SWE-bench Participation Infrastructure — stdlib Skill + Batch Execution

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Enable Reyn to participate in SWE-bench Verified (the de facto standard benchmark for coding agents).
The required capabilities (file edit / shell / git) are already covered by existing ops. The only
additions are (A) a `swe_bench` stdlib skill and (B) a `reyn eval benchmark` batch execution command.

---

## Motivation

### SWE-bench's Industry Position

SWE-bench Verified is, as of 2026, the de facto evaluation standard for coding agents.
Major frameworks and model vendors compete on scores, and **being able to participate in it
is itself a proof of credibility at OSS launch time**.

Since Claude Opus 4.7 has nearly reached the ceiling at 87.6% (2026-04), it can be used less
as a model performance comparison and more as a demonstration that "Reyn's architecture works
for real production coding tasks."

### Reyn Already Has What It Takes

Capabilities required by SWE-bench:

| Requirement | Reyn's coverage |
|---|---|
| Reading code files | `read_file` op ✅ |
| Editing files | `edit_file` op ✅ |
| Running tests | `shell` op ✅ |
| Getting git diffs | `shell` op (`git diff`) ✅ |
| Grepping a repository | `grep` op ✅ |

No OS changes needed. Implementable as a skill (P7 compliant).

---

## How SWE-bench Works

```
SWE-bench harness
  → passes task data (instance_id, repo, base_commit, problem_statement)
  → Reyn runs the swe_bench skill
  → outputs a git patch (diff)
  → harness applies the patch and runs tests
  → judges pass / fail
```

Reyn's entry point:

```
# Single task
reyn run swe_bench --input instance.json --output patch.diff

# Batch (500 problems)
reyn eval benchmark swe_bench --tasks swe_bench_verified.jsonl --output results/
```

---

## Proposed implementation

### Component A — `swe_bench` stdlib Skill (MEDIUM)

```
src/reyn/stdlib/skills/swe_bench/
  skill.md
  phases/
    setup.md          ← Check out the repository at base_commit
    explore.md        ← Read problem_statement, locate relevant code via grep
    plan.md           ← Decide on a fix strategy
    apply.md          ← Implement changes with edit_file / write_file
    verify.md         ← Run failing tests with shell and confirm they pass
    report.md         ← Generate git diff and format final output
```

**skill.md frontmatter skeleton**:

```yaml
---
name: swe_bench
description: Solve a SWE-bench task — code fix and verification for a GitHub issue
entry_phase: setup
graph:
  setup:     [explore]
  explore:   [plan]
  plan:      [apply]
  apply:     [verify, plan]   # Return to plan if tests fail
  verify:    [report, apply]  # Return to apply if verification fails
  report:    []               # Terminal
final_output_schema: swe_bench_result
input_schema:
  instance_id: string
  repo: string
  base_commit: string
  problem_statement: string
  hints_text: string          # optional
  test_patch: string          # Evaluation tests (run only, must not be edited)
permissions:
  file:
    read: ["*"]
    write: ["*"]              # Writes to the entire repository are required
  shell: true                 # git / test runner execution
---
```

**Role of each phase**:

`setup` — Check out the repository at base_commit and prepare the test environment
(using `git checkout <base_commit>` via the shell op).

`explore` — Identify files and functions to fix from the `problem_statement`.
Search related code with the `grep` op and collect context with `read_file`.
Save results to `exploration.md` in the workspace.

`plan` — Draft a fix plan based on the exploration results.
Save the target files and intended changes to `plan.md`.

`apply` — Implement changes with `edit_file` / `write_file` following `plan.md`.
Fix one file at a time and perform basic syntax checks.

`verify` — Run the tests from `test_patch` with the `shell` op.
All tests pass → go to `report`. Failure → return to `apply` (up to `max_retries: 3`).

`report` — Run `git diff HEAD` to generate the patch.
Store in `final_output` in the format expected by SWE-bench.

**final_output_schema**:

```python
class SweBenchResult(BaseModel):
    instance_id: str
    patch: str          # Output of git diff
    tests_passed: bool
    attempts: int       # Number of verify loop iterations
```

### Component B — `reyn eval benchmark` Batch Execution Command (MEDIUM)

A batch runner for efficiently executing all 500 problems in SWE-bench Verified.

```
reyn eval benchmark <skill_name> \
  --tasks swe_bench_verified.jsonl \
  --output results/ \
  --concurrency 4 \
  [--limit 50]              # Try a subset first
  [--resume]                # Resume from a checkpoint
```

**Input JSONL format** (using the official SWE-bench dataset format as-is):

```jsonl
{"instance_id": "django__django-1234", "repo": "django/django", "base_commit": "abc123", "problem_statement": "...", "hints_text": "...", "test_patch": "..."}
```

**Output directory structure**:

```
results/
  run_<timestamp>/
    summary.json          ← Overall pass rate / execution time / cost aggregation
    patches/
      django__django-1234.diff
      ...
    logs/
      django__django-1234.jsonl  ← P6 event log (per instance)
```

**`--resume` behavior**:
Reads completed instance_ids from `results/run_<timestamp>/summary.json` and
runs only the remaining ones (for resuming after interruption).

**summary.json format**:

```json
{
  "run_id": "run_20260510_093000",
  "skill": "swe_bench",
  "total": 500,
  "completed": 423,
  "passed": 371,
  "pass_rate": 0.877,
  "total_cost_usd": 142.30,
  "avg_cost_per_instance": 0.34,
  "avg_attempts": 1.8
}
```

### Connecting to the SWE-bench Harness

The official SWE-bench evaluation runs inside a Docker container. Connection methods:

**Method 1: Direct CLI execution (recommended)**

```bash
# Wrapper script called by the SWE-bench harness
reyn run swe_bench \
  --input '{"instance_id": "...", "repo": "...", ...}' \
  --output-field patch \
  > patch.diff
```

**Method 2: Via A2A endpoint**

```
reyn web  # Start on localhost:8080
# harness sends POST /a2a/agents/swe_bench with message/send
```

The A2A endpoint (`reyn web`) is an existing implementation and requires no additional changes.

---

## Relationship with FP-0007

| FP | Relationship |
|---|---|
| FP-0007 (Evaluation Infrastructure) | The `reyn eval benchmark` batch runner is an extension of Component B (`reyn eval run`). `reyn eval benchmark` handles N problems, while `reyn eval run` handles 1 skill × M test cases |
| FP-0007 Component A (export) | Exporting batch run P6 logs to Langfuse enables visualization of which phases fail most often |

---

## Dependencies

- `src/reyn/stdlib/skills/` — add `swe_bench/` (no OS changes)
- `src/reyn/cli/eval.py` — add `benchmark` subcommand (same file as FP-0007 Component B)
- `src/reyn/op_runtime/shell.py` — shell op (existing, no changes)
- FP-0007: `reyn eval benchmark` is implemented in the same file as FP-0007's eval.py, so
  simultaneous or post-FP-0007 release is preferable. Independent implementation is possible.

No prerequisite PRs: the swe_bench skill can be implemented independently with no OS changes.

---

## Cost estimate

**Total: LARGE**

| Task | Cost | Notes |
|---|---|---|
| Component A: `swe_bench` skill (6 phases) | MEDIUM | Phase design + tuning each phase instruction |
| Component A: `apply` / `verify` loop tuning | MEDIUM | Retry limit and regression detection behavior verification |
| Component B: `reyn eval benchmark` CLI | MEDIUM | concurrency / resume / summary.json output |
| SWE-bench harness integration verification | SMALL | CLI wrapper + A2A connection test |
| **Total** | **LARGE** | Bottleneck is verify loop quality (directly impacts pass rate) |

---

## Expected Outcomes

| Metric | Target |
|---|---|
| Pass rate (SWE-bench Verified) | 40%+ (with frontier model, equivalent to Hermes) |
| Cost / instance | $0.30–0.50 (using flash model) |
| Effect on OSS launch | A track record of "running SWE-bench with Reyn" contributes to ecosystem credibility |

---

## Related

- `src/reyn/stdlib/skills/skill_improver/` — reference for multi-phase skill implementation
- `src/reyn/op_runtime/shell.py` — shell op (used for git / test runner execution)
- FP-0007 (`0007-evaluation-infrastructure.md`) — shared foundation for eval CLI and export
- FP-0006 (`0006-skill-self-improvement.md`) — a future path to self-improve the swe_bench skill itself
- [SWE-bench Verified](https://www.swebench.com/)
- [SWE-bench GitHub](https://github.com/princeton-nlp/SWE-bench)
