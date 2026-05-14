---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn eval]
---

# `reyn eval`

Run an eval spec against a target skill non-interactively. Each case is judged phase-by-phase against rubric criteria; per-case results plus an overall summary are written to `.reyn/eval_reports/`.

## Synopsis

```
reyn eval [OPTIONS] FILE
```

## Positional arguments

| Name | Description |
|------|-------------|
| `FILE` | Path to the eval spec markdown (e.g. `reyn/local/my_skill/eval.md`). The spec references the target skill via its `skill_dsl_path` frontmatter field. |

## Options

| Flag | Description |
|------|-------------|
| `--model MODEL` | Model class (`light`/`standard`/`strong`) or LiteLLM model string. **Precedence:** CLI > spec > `reyn.yaml`. |
| `--dsl-root DIR` | DSL root override for the target skill. Inferred from the skill path by default. |
| `--output-language LANG` | Output language code passed to both eval skill and target skill. Default from `reyn.yaml`. |
| `--max-phase-visits N` | Cap on single-phase revisits per case. `0` = unlimited. Default from `reyn.yaml` or `25`. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | All cases passed |
| `1` | Spec failed to load (e.g. malformed eval.md) |
| `2` | One or more cases failed their criteria |

## Output

A summary line per case is printed to stdout:

```
━━━ case: short_summary ━━━
  input: reyn is a workflow OS for LLMs.
  ✓ score=0.95  (4/4 required)
```

The full structured report is written to `.reyn/eval_reports/<target_skill>/<timestamp>.json` and the path is printed on the final line.

## Non-interactive constraint

`reyn eval` does not prompt. Every permission the target skill needs must be pre-approved:

- run the target once interactively (`reyn run <target> "<sample>"`) and accept the prompts — choices persist to `.reyn/approvals.yaml`, OR
- set project-wide grants in `reyn.yaml`:

```yaml
permissions:
  python.pure: allow
  python.trusted: allow   # also requires --allow-untrusted-python at runtime
```

Without prior approval the target run fails and the case is reported as not-finished. The framing reads as a target-skill bug, but the cause is missing approvals.

## Examples

Run the eval bundled with a project skill:

```bash
reyn eval reyn/project/article_writer/eval.md
```

Override the model just for this run:

```bash
reyn eval reyn/local/my_skill/eval.md --model strong
```

Iterate during development (use a cheap model, single case):

```bash
reyn eval reyn/local/my_skill/eval.md --model light
```

## See also

- [run.md](run.md) — `reyn run` (the underlying execution path)
- [Reference: stdlib/eval](../stdlib/eval.md) — what the eval skill produces
- [Reference: stdlib/eval_builder](../stdlib/eval_builder.md) — generate spec files
- [Reference: permissions](../config/permissions.md) — pre-approval mechanics

---

## `reyn eval run` — golden dataset runner (FP-0007)

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

## `reyn eval report` — result summary (FP-0007)

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

## See also (FP-0007 additions)

- [Concepts: evaluation](../../concepts/evaluation.md) — architecture overview and competitive comparison
- [Guide: Setup evaluation](../../guide/evaluation.md) — quickstart, export backends, CI integration
- [Reference: `reyn.yaml`](../config/reyn-yaml.md) — `eval.exporters` configuration
- [Reference: control-ir](../runtime/control-ir.md) — `judge_output` op schema
