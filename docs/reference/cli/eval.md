---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn eval]
---

# `reyn eval`

Evaluate a skill. Subcommands:

| Subcommand | Description |
|------------|-------------|
| `run` | Run a skill against a golden JSONL dataset; gate CI on pass rate |
| `report` | Summarise past `reyn eval run` results for a skill |
| `compare` | Compare pass rate across two skill versions using P6 event log |
| `benchmark` | Run a skill across a JSONL task file with concurrent dispatch; used by SWE-bench harness |
| `spec` | Legacy: run an `eval.md` spec file non-interactively (backward compat) |

## Synopsis

```
reyn eval run       <SKILL_NAME> [OPTIONS]
reyn eval report    <SKILL_NAME> [OPTIONS]
reyn eval compare   <SKILL_NAME> [OPTIONS]
reyn eval benchmark <SKILL_NAME> --tasks PATH --output DIR
                                 [--concurrency N] [--limit N] [--resume]
                                 [--model MODEL] [common flags]
reyn eval spec      FILE [OPTIONS]
```

## Subcommand: `benchmark`

Run a skill against a JSONL task file with concurrent dispatch. Each line of the task file is one task input matching the skill's `input_schema`.

```
reyn eval benchmark <SKILL_NAME> --tasks PATH --output DIR [OPTIONS]
```

| Flag | Description |
|---|---|
| `<SKILL_NAME>` | Skill name to run (resolved via reyn/project → local → stdlib) |
| `--tasks PATH` | **required** — JSONL task file; each line = one task input |
| `--output DIR` | **required** — output directory; results written under `<DIR>/run_<timestamp>/` |
| `--concurrency N` | Max concurrent skill runs (default: `4`) |
| `--limit N` | Stop after the first N tasks (applied after `--resume` filtering) |
| `--resume` | Resume from latest prior run in `<output>`; skip already-completed tasks |
| `--model MODEL` | Model override (default: from `reyn.yaml`) |

The benchmark dispatcher uses workspace-isolated runs (= `_benchmark_isolated_workspace` in `cli/commands/eval_benchmark.py`) so concurrent task runs do not collide on cwd/files.

## Non-interactive constraint

All `reyn eval` subcommands are non-interactive — they do not prompt. Every permission the target skill needs must be pre-approved:

- run the target once interactively (`reyn run <target> "<sample>"`) and accept the prompts — choices persist to `.reyn/approvals.yaml`, OR
- set project-wide grants in `reyn.yaml`:

```yaml
permissions:
  python.safe: allow
  python.unsafe: allow   # also requires --allow-unsafe-python at runtime
```

Without prior approval the target run fails and the case is reported as not-finished.

## See also

- [run.md](run.md) — `reyn run` (the underlying execution path)
- [Reference: stdlib/eval](../stdlib/eval.md) — what the eval skill produces
- [Reference: stdlib/eval_builder](../stdlib/eval_builder.md) — generate spec files
- [Reference: permissions](../config/permissions.md) — pre-approval mechanics

---

## `reyn eval run` — golden dataset runner

Run a skill against a JSONL golden dataset and gate CI on pass rate.

### Synopsis

```
reyn eval run <SKILL_NAME> --dataset <FILE> [OPTIONS]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `SKILL_NAME` | Name of the skill to evaluate (resolved via the standard skill lookup order). |

### Options

| Flag | Description |
|------|-------------|
| `--dataset FILE` | Path to the golden JSONL dataset. **Required.** Each line must be a JSON object with an `input` field; `expected` and `tags` are optional. |
| `--threshold FLOAT` | Minimum pass rate (0.0–1.0) for exit code 0. Default: `0.0` (all results recorded, never fails on rate). |
| `--tags TAG[,TAG...]` | Run only cases whose `tags` array contains at least one of the given tags. |
| `--mode MODE` | Comparison mode: `judge` (default, LLM-scored via `judge_output`) or `exact` (exact JSON match against `expected`). |
| `--model MODEL` | Model class (`light`/`standard`/`strong`) or LiteLLM model string. Default from `reyn.yaml`. |
| `--output-language LANG` | Output language code passed to the skill. Default from `reyn.yaml`. |
| `--max-phase-visits N` | Cap on single-phase revisits per case. `0` = unlimited. Default from `reyn.yaml` or `25`. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Pass rate is at or above `--threshold` (or no threshold set). |
| `1` | Dataset file not found, malformed JSONL, or skill not found. |
| `2` | Pass rate is below `--threshold`. |

### Output

A summary line per case is printed to stdout:

```
=== Eval: my_skill [3 case(s)] ===
    model=standard

━━━ case: smoke/0 ━━━
  input: Summarise async programming
  ✓ score=0.91  passed

━━━ case: edge-case/empty-input ━━━
  input: (empty)
  ✗ score=0.31  failed

═══════════════════════════════════════════════════════
 ✗ 2/3 cases passed (66.7%)  threshold=0.8
 Results → .reyn/eval-results/my_skill/2026-05-14T12:00:00.jsonl
═══════════════════════════════════════════════════════
```

The full structured result is written to `.reyn/eval-results/<skill>/<timestamp>.jsonl`. Each line records one case result including the case input, expected, actual `final_output`, score, passed flag, and `skill_version_hash`.

### Workspace isolation

Each case runs in an isolated workspace copy. Production workspace state (indexed sources, approvals, existing artifacts) is not visible to eval cases. Results from one case do not affect the next.

### Non-interactive constraint

`reyn eval run` does not prompt. All permissions the skill needs must be pre-approved. See the [non-interactive pre-approval guide](../../guide/evaluation.md#non-interactive-permissions) or [Reference: permissions](../config/permissions.md).

### Examples

```bash
# Run against a golden dataset, fail CI if pass rate drops below 80%
reyn eval run my_skill --dataset eval/golden.jsonl --threshold 0.8

# Run smoke-tagged cases only
reyn eval run my_skill --dataset eval/golden.jsonl --tags smoke --threshold 1.0

# Use exact-match comparison (requires 'expected' in every dataset line)
reyn eval run my_skill --dataset eval/golden.jsonl --mode exact

# Cheap model for fast iteration during development
reyn eval run my_skill --dataset eval/golden.jsonl --model light
```

---

## `reyn eval report` — result summary

Summarise past `reyn eval run` results for a skill.

### Synopsis

```
reyn eval report <SKILL_NAME> [OPTIONS]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `SKILL_NAME` | Name of the skill whose results to show. |

### Options

| Flag | Description |
|------|-------------|
| `--limit N` | Number of most recent runs to show. Default: `10`. |
| `--json` | Output as a JSON array instead of the default table. |
| `--dataset FILE` | Filter to runs that used this dataset file. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Results found and displayed (including the no-results case). |
| `1` | Skill not found or I/O error reading `.reyn/eval-results/`. |

### Output

Default table format:

```
my_skill — 3 runs on record

  2026-05-14  dataset=eval/golden.jsonl  2/3 passed (66.7%)  model=standard
  2026-05-13  dataset=eval/golden.jsonl  3/3 passed (100%)   model=standard
  2026-05-12  dataset=eval/golden.jsonl  1/3 passed (33.3%)  model=light
```

If no results are recorded:

```
No eval results found for 'my_skill'.
Try: reyn eval run my_skill --dataset eval/golden.jsonl
```

### Examples

```bash
# Show the 10 most recent runs
reyn eval report my_skill

# Machine-readable output
reyn eval report my_skill --json

# Filter to runs against a specific dataset
reyn eval report my_skill --dataset eval/golden.jsonl --limit 5
```

---

## `reyn eval compare` — version regression comparison

Compare a skill's pass rate across two versions using the P6 event log. No additional skill executions are required — results are aggregated from existing `run_skill_started` events whose `skill_version_hash` field matches the specified versions.

### Synopsis

```
reyn eval compare <SKILL_NAME> [OPTIONS]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `SKILL_NAME` | Name of the skill to compare (resolved via the standard skill lookup order). |

### Options

| Flag | Description |
|------|-------------|
| `--baseline HASH_OR_LABEL` | The hash prefix or label for the baseline version. Auto-selected as the second-most-recent hash when omitted (see auto-baseline rule below). |
| `--candidate HASH_OR_LABEL` | The hash prefix or label for the candidate version. Auto-selected as the most-recent hash when omitted. |
| `--threshold FLOAT` | Delta below which a regression alert (exit code 1) is triggered. Default: `0.05` (5 percentage-point drop triggers alert). |
| `--format FORMAT` | Output format: `text` (default) or `json`. |
| `--dataset FILE` | Filter to runs that used a specific golden dataset. Optional. |
| `--since DATE` | Only consider runs on or after this ISO date. Optional. |

### Auto-baseline selection rule

When `--baseline` is omitted, `reyn eval compare` reads the `skill_version_hash` values from `.reyn/events/*.jsonl` for the target skill, orders them by first-seen timestamp, and uses:

- **candidate** = most-recently-seen hash
- **baseline** = second-most-recently-seen hash

If fewer than two distinct hashes exist in the log, the command exits with code 2 and an explanatory message.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Candidate pass rate is at or above `baseline − threshold`. No regression. |
| `1` | Regression alert: candidate pass rate dropped by more than `--threshold` relative to baseline. |
| `2` | Error: skill not found, insufficient version history, or I/O failure. |

### Text format example

```
reyn eval compare my_skill

  Skill:     my_skill
  Baseline:  sha:abc12345  (72% pass, 36/50 runs)  2026-05-01 ~ 2026-05-05
  Candidate: sha:def67890  (88% pass, 44/50 runs)  2026-05-05 ~ 2026-05-15
  Delta:     +16pp  /  threshold=-5pp
  Result:    OK — no regression
```

### JSON format example

```bash
reyn eval compare my_skill --format json
```

```json
{
  "skill": "my_skill",
  "baseline": {
    "hash": "abc123456789abcdef...",
    "pass_rate": 0.72,
    "run_count": 50,
    "date_range": ["2026-05-01", "2026-05-05"]
  },
  "candidate": {
    "hash": "def678901234567890...",
    "pass_rate": 0.88,
    "run_count": 50,
    "date_range": ["2026-05-05", "2026-05-15"]
  },
  "delta_pp": 16.0,
  "threshold_pp": -5.0,
  "regression": false
}
```

### Cross-reference: `skill_version_hash`

`reyn eval compare` relies on the `skill_version_hash` field in every `run_skill_started` event — the sha256 of the skill's `skill.md` at the time of execution. See [skill self-improvement](../../deep-dives/proposals/0006-skill-self-improvement.md) for the field contract and [Reference: events](../runtime/events.md) for the event envelope.

### Examples

```bash
# Auto-select baseline and candidate
reyn eval compare my_skill

# Compare two specific hashes
reyn eval compare my_skill --baseline abc123 --candidate def456

# Fail if pass rate drops more than 10pp
reyn eval compare my_skill --threshold 0.10

# Machine-readable output for CI
reyn eval compare my_skill --format json --threshold 0.05
```

---

## `reyn eval spec` — legacy spec runner

Run an `eval.md` spec file against a target skill non-interactively. Each case is judged phase-by-phase against rubric criteria; per-case results and an overall summary are written to `.reyn/eval-results/`.

### Synopsis

```
reyn eval spec FILE [OPTIONS]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `FILE` | Path to the eval spec markdown (e.g. `reyn/local/my_skill/eval.md`). The spec references the target skill via its `skill_dsl_path` frontmatter field. |

### Options

| Flag | Description |
|------|-------------|
| `--model MODEL` | Model class (`light`/`standard`/`strong`) or LiteLLM model string. **Precedence:** CLI > spec > `reyn.yaml`. |
| `--dsl-root DIR` | DSL root override for the target skill. Inferred from the skill path by default. |
| `--output-language LANG` | Output language code passed to both eval skill and target skill. Default from `reyn.yaml`. |
| `--max-phase-visits N` | Cap on single-phase revisits per case. `0` = unlimited. Default from `reyn.yaml` or `25`. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All cases passed |
| `1` | Spec failed to load (e.g. malformed eval.md) |
| `2` | One or more cases failed their criteria |

### Examples

```bash
reyn eval spec reyn/project/article_writer/eval.md
reyn eval spec reyn/local/my_skill/eval.md --model strong
```

---

## See also

- [run.md](run.md) — `reyn run` (the underlying execution path)
- [Concepts: evaluation](../../concepts/observability/evaluation.md) — architecture overview and competitive comparison
- [Guide: Setup evaluation](../../guide/evaluation.md) — quickstart, export backends, CI integration
- [Reference: `reyn.yaml`](../config/reyn-yaml.md) — `eval.exporters` configuration
- [Reference: control-ir](../runtime/control-ir.md) — `judge_output` op schema
- [Reference: stdlib/eval](../stdlib/eval.md) — what the eval skill produces
- [Reference: permissions](../config/permissions.md) — pre-approval mechanics
