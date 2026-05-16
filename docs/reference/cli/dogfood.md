---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn dogfood]
---

# `reyn dogfood`

Scenario-based regression testing for the chat router (FP-0036). Declare expected behaviour in YAML scenario sets, run them against the real system, and track pass/fail across releases.

## Synopsis

```
reyn dogfood run <SET_YAML> [OPTIONS]
reyn dogfood coverage [--feature-map FILE] [--json] [<SET_YAML>...]
reyn dogfood report <RUN_ID> [--json]
reyn dogfood compare <BASELINE> <CANDIDATE> [--threshold FLOAT] [--json]
reyn dogfood baseline <RUN_ID> [--label NAME]
reyn dogfood publish <RUN_ID> [--repo OWNER/REPO] [--category SLUG] [--dry-run] [--template PATH] [--batch-id N] [--topic TOPIC]
```

## Description

`reyn dogfood` drives the chat router with structured scenario sets — each scenario declares an input prompt and expected behaviour across three observation surfaces:

- **reply** — natural language output (judge / substring / regex)
- **events** — P6 event log (must_emit / must_not_emit)
- **artifacts** — workspace artifacts produced by the run

Each scenario returns a 4-band outcome: `verified | inconclusive | refuted | blocked`. Outcomes are tracked across runs so regressions are surfaced automatically with `reyn dogfood compare`.

## Storage layout

```
.reyn/dogfood/
  runs/<run_id>/
    scenarios/<scenario_id>/
      output.json       # reply + verifier verdicts
      events.jsonl      # captured P6 event tail
      artifacts/        # workspace snapshot
    summary.json        # 4-band aggregate + Brier score
  baselines/<label>/    # symlink to a named baseline run
```

## Outcome scale

| Outcome | Meaning |
|---------|---------|
| `verified` | All verifiers passed. |
| `inconclusive` | Verifiers could not determine pass or fail (e.g. judge uncertainty). |
| `refuted` | At least one verifier failed. |
| `blocked` | The scenario could not run (e.g. permission denied, agent error). |

Outcome ordering (worst to best): `blocked < refuted < inconclusive < verified`.

---

## `reyn dogfood run` — execute a scenario set

Run every scenario in the YAML file through the chat router and record results.

### Synopsis

```
reyn dogfood run <SET_YAML> [OPTIONS]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `SET_YAML` | Path to a scenario set YAML file (e.g. `dogfood/scenarios/chat_router_smoke.yaml`). |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--n N` | `1` | Number of repetitions. Use N ≥ 3 for stability bands; worst-case outcome wins across repetitions. |
| `--replay FIXTURE_DIR` | — | Run in replay mode using recorded LLM fixtures. No live LLM calls are made. |
| `--agent NAME` | `default` | Chat-router agent name. |
| `--storage DIR` | `.reyn/dogfood/runs/<run_id>` | Override the run output directory. |
| `--run-id RUN_ID` | *(auto UUID)* | Explicit run ID. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Run completed (any outcome distribution). |
| `2` | Error: scenario file not found, dependency not available. |

### Output

```
dogfood run: chat_router_smoke  (3 scenarios, n=1)

  run_id      : a1b2c3d4-...
  verified    : 2
  inconclusive: 1
  refuted     : 0
  blocked     : 0
  total       : 3
  verified %  : 66.7%
  Brier       : 0.1200

  results → .reyn/dogfood/runs/a1b2c3d4-.../summary.json
```

### Examples

```bash
# Single run
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml

# 5 repetitions for stability
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --n 5

# Deterministic replay (no LLM cost)
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --replay dogfood/fixtures/chat_router_smoke/

# Custom storage
reyn dogfood run dogfood/scenarios/chat_router_smoke.yaml --storage /tmp/my_run
```

---

## `reyn dogfood coverage` — feature-map coverage

Show which feature-map features are covered by scenario sets.

### Synopsis

```
reyn dogfood coverage [--feature-map FILE] [--json] [<SET_YAML>...]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `SET_YAML...` | Zero or more scenario set YAML files. Defaults to `dogfood/scenarios/*.yaml` if omitted. |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--feature-map FILE` | `docs/feature-map.md` | Path to the feature map Markdown file. |
| `--json` | — | Output coverage as JSON. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Coverage computed successfully. |
| `2` | Error: missing files, F4 module not available. |

### Examples

```bash
# Default: all scenario sets, default feature map
reyn dogfood coverage

# Specific file + JSON output
reyn dogfood coverage dogfood/scenarios/chat_router_smoke.yaml --json

# Custom feature map
reyn dogfood coverage --feature-map docs/my-feature-map.md
```

---

## `reyn dogfood report` — print stored run results

Print the 4-band breakdown and Brier score from a previous run.

### Synopsis

```
reyn dogfood report <RUN_ID> [--json]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `RUN_ID` | Run ID (UUID) or path to the run directory. |

### Options

| Flag | Description |
|------|-------------|
| `--json` | Emit the report as JSON. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Report printed. |
| `2` | Run directory not found or summary.json missing. |

### Output

```
Run: a1b2c3d4-...
Set: chat_router_smoke
Started: 2026-05-16T10:00:00+00:00
Completed: 2026-05-16T10:02:15+00:00

  verified    : 2
  inconclusive: 1
  refuted     : 0
  blocked     : 0
  total       : 3
  verified %  : 66.7%
  Brier       : 0.1200

Scenarios:
  ✓ simple_greeting                            verified
  ? complex_multi_turn                         inconclusive
  ✓ skill_dispatch_smoke                       verified
```

### Examples

```bash
reyn dogfood report a1b2c3d4-1234-...
reyn dogfood report a1b2c3d4-1234-... --json
reyn dogfood report .reyn/dogfood/runs/a1b2c3d4-1234-...
```

---

## `reyn dogfood compare` — regression diff

Compare a candidate run against a baseline. Exits 1 if the verified-rate drop exceeds `--threshold`.

### Synopsis

```
reyn dogfood compare <BASELINE> <CANDIDATE> [--threshold FLOAT] [--json]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `BASELINE` | Baseline run ID (or path). |
| `CANDIDATE` | Candidate run ID (or path). |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--threshold FLOAT` | `0.05` | Verified-rate drop that triggers a regression alert. Default: 0.05 = 5 percentage points. |
| `--json` | — | Emit comparison as JSON. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | No regression detected (or delta within threshold). |
| `1` | Regression alert: verified-rate dropped by more than `--threshold`. |
| `2` | Error: run directories not found. |

### Output

```
  Baseline:  a1b2c3d4-...  (66.7% verified)
  Candidate: b2c3d4e5-...  (33.3% verified)
  Delta:     -33.4pp  /  threshold=-5.0pp
  Result:    REGRESSION ALERT

Regressed scenarios (1):
  - complex_multi_turn: verified → refuted
```

### Examples

```bash
# Compare two runs
reyn dogfood compare a1b2c3d4-... b2c3d4e5-...

# Stricter threshold (10pp)
reyn dogfood compare a1b2c3d4-... b2c3d4e5-... --threshold 0.10

# CI: JSON output + exit code 1 on regression
reyn dogfood compare baseline_run candidate_run --json; echo "exit: $?"

# Using a named baseline
reyn dogfood compare .reyn/dogfood/baselines/v1.2-stable b2c3d4e5-...
```

---

## `reyn dogfood baseline` — tag a run as a named baseline

Create a symlink under `.reyn/dogfood/baselines/<label>/` pointing at a stored run.

### Synopsis

```
reyn dogfood baseline <RUN_ID> [--label NAME]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `RUN_ID` | Run ID (or path) to mark as a baseline. |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--label NAME` | *(run_id)* | Human-readable label for the baseline (e.g. `v1.2-stable`). |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Baseline created (or overwritten). |
| `2` | Run directory not found. |

### Examples

```bash
# Tag a run with the default label (= run_id)
reyn dogfood baseline a1b2c3d4-...

# Tag a run with a release label
reyn dogfood baseline a1b2c3d4-... --label v1.2-stable

# Use in compare
reyn dogfood compare v1.2-stable b2c3d4e5-...
```

---

## Scenario set YAML format

Scenario sets are YAML files that declare a set of chat-router scenarios:

```yaml
type: dogfood_scenario_set
name: chat_router_smoke
description: Chat router intent dispatch smoke test
covers:
  - chat-router/intent-routing
  - stdlib-skill/direct_llm

scenarios:
  - id: simple_greeting
    covers: [chat-router/intent-routing, stdlib-skill/direct_llm]
    input: "こんにちは、何ができますか?"
    expected:
      reply:
        kind: judge
        rubric:
          - explains capabilities at high level
          - mentions chat / skills / agents
      events:
        must_emit:
          - { type: skill_run_spawned, count: ">=1" }
          - { type: skill_run_completed, status: success }
        must_not_emit:
          - { type: permission_denied }
      artifacts:
        - { skill: direct_llm, present: true }
      outcome_prediction:
        verified: 0.7
        inconclusive: 0.2
        refuted: 0.05
        blocked: 0.05
```

`outcome_prediction` enables Brier score tracking — declare your confidence in each band and the framework measures calibration over time.

---

## `reyn dogfood publish` — publish a batch Discussion to GitHub

Read a stored run's `summary.json`, render the Discussion body from the Markdown template, and create a thread in the configured GitHub Discussions category.

**Authentication**: set `GH_TOKEN` or `GITHUB_TOKEN` env var (same convention as the `gh` CLI). The command exits with an error if neither is set and `--dry-run` is not passed.

### Synopsis

```
reyn dogfood publish <RUN_ID> [--repo OWNER/REPO] [--category SLUG] \
                               [--dry-run] [--template PATH] \
                               [--batch-id N] [--topic TOPIC]
```

### Positional arguments

| Name | Description |
|------|-------------|
| `RUN_ID` | Run ID (UUID) or path to the run directory. |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--repo OWNER/REPO` | `tya5/reyn` (or detected from `git remote`) | GitHub repository to post the Discussion in. |
| `--category SLUG` | `dogfood-batches` | Discussion category slug. |
| `--dry-run` | — | Render the title and body to stdout without posting to GitHub. |
| `--template PATH` | `docs/deep-dives/contributing/templates/dogfood-discussion-template.md` | Override the template file. |
| `--batch-id N` | *(from summary.json)* | Batch number override (required if `summary.json` lacks `batch_id`). |
| `--topic TOPIC` | *(from summary.json)* | Short topic string override (required if `summary.json` lacks `topic`). |

### Authentication

`GH_TOKEN` takes precedence over `GITHUB_TOKEN`. The token must have the `write:discussion` scope (or `repo` scope for private repositories).

```bash
export GH_TOKEN="ghp_..."
reyn dogfood publish <RUN_ID>
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Discussion created (or dry-run completed). |
| `1` | GitHub API error (network, auth, GraphQL error). |
| `2` | Error: run directory not found, summary.json missing, template not found. |

### Title format

```
Batch <N> (YYYY-MM-DD): <topic> — <verified_pct>% verified, <regressed_count> regressed
```

Example:

```
Batch 27 (2026-05-17): chat router smoke + stdlib core — 75% verified, 1 regressed
```

### Output

```
Discussion created: https://github.com/tya5/reyn/discussions/42
  Title  : Batch 27 (2026-05-17): chat router smoke — 75% verified, 1 regressed
  Number : #42
```

### Examples

```bash
# Dry-run: see the rendered body without posting
reyn dogfood publish a1b2c3d4-... --dry-run

# Post to default repo (tya5/reyn) with batch-id + topic overrides
reyn dogfood publish a1b2c3d4-... --batch-id 27 --topic "chat router smoke"

# Post to a fork
reyn dogfood publish a1b2c3d4-... --repo acme/reyn-fork

# Use a custom template
reyn dogfood publish a1b2c3d4-... --template path/to/my-template.md

# Dry-run from a full path
reyn dogfood publish .reyn/dogfood/runs/a1b2c3d4-... --dry-run
```

---

## Related

- [Reference: `reyn eval compare`](eval.md) — per-skill rubric regression (orthogonal surface)
- [Reference: `reyn run`](run.md) — headless skill execution (same Agent.run path)
- [Concepts: events](../../concepts/events.md) — P6 event log
- [Deep dive: dogfood discipline](../../deep-dives/contributing/dogfood-discipline.md) — 4-band outcome + 9-principle framework
- [Deep dive: dogfood reporting](../../deep-dives/contributing/dogfood-reporting.md) — Discussion format + issue filing guide
- [Proposal: FP-0036](../../deep-dives/proposals/0036-dogfood-scenario-framework.md) — full design spec
