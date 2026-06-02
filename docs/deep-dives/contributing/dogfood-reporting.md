---
type: contributing
topic: dogfood-reporting
audience: [human, agent]
---

# Dogfood Batch Reporting

Sister document to [`dogfood-discipline.md`](dogfood-discipline.md) — that doc covers the 9-principle framework and the A1–A5 iterative loop. This doc covers **where results are recorded** and **how they are surfaced to the team**.

---

## 1. Three reporting layers

Every batch produces output at three distinct layers. Each layer serves a different purpose and a different audience.

| Layer | Location | Purpose | Audience |
|-------|----------|---------|----------|
| **Detail data** | `docs/deep-dives/journal/dogfood/<batch-dir>/` | Full record: metrics, per-scenario verdicts, lessons | Maintainers, future batches, agents |
| **GitHub headline** | GitHub Discussions → `Dogfood batches` | Surface results to the team; anchor discussion | Team, stakeholders |
| **Actionable findings** | GitHub Issues with label `dogfood-finding` | Track bugs with severity; link to fix PRs | Maintainers, fix-wave agents |

The three layers are mutually reinforcing: the Discussion headline links to the journal commit; each Issue links back to the Discussion thread; the journal retrospective references both.

---

## 2. Per-batch journal entry (detail data)

### Location

```
docs/deep-dives/journal/dogfood/<YYYY-MM-DD-batch-N-<topic>>/
```

Examples:

```
docs/deep-dives/journal/dogfood/2026-05-16-batch-26-fp-0034-n5-stability/
docs/deep-dives/journal/dogfood/2026-05-17-batch-27-chat-router-smoke/
```

The directory name encodes `date + batch number + topic-slug`. The topic slug is a brief descriptor of what the batch tested — it does not need to be globally unique beyond the date.

### Files

#### `summary.md` — headline metrics (30–50 lines)

The summary is the first file a reader opens. It contains the batch identity, headline metrics, framework version, and baseline comparison. It should be readable in under two minutes.

```markdown
# Batch 27 — Summary

**Date**: 2026-05-17
**Batch ID**: B27
**Topic**: chat router smoke + stdlib core
**Framework version**: FP-0036 `<commit_hash>`
**Scenario sets**: chat_router_smoke (7 scenarios) + stdlib_skills_core (9 scenarios)

---

## Headline metrics

| Metric | Value |
|--------|-------|
| Total runs | 16 (= 16 scenarios × N=1) |
| Verified | 12 / 16 = 75% |
| Inconclusive | 3 |
| Refuted | 1 |
| Blocked | 0 |
| Brier score | 0.21 |
| Wall-clock | ~12 min |
| LLM cost (est) | ~$0.04 |

## Baseline comparison

| Metric | Baseline (B26) | This batch (B27) | Delta |
|--------|---------------|-----------------|-------|
| Verified % | 91.4% | 75% | -16.4pp |
| Brier | 0.177 | 0.21 | +0.033 |
| Regressed scenarios | — | 1 (`simple_capability_question`) | — |

> **Note**: B27 covers a different scenario set than B26 (chat_router_smoke vs FP-0034 wrapper-only).
> The verified-rate drop reflects new scenario coverage, not a regression in the B26 scenarios.

## Carry-over from B26

- B26-S3-NOOP-1 (invoke_action visibility gap) — LOW priority, deferred

## Next steps

- File `[dogfood B27]` issue for `simple_capability_question` refuted outcome
- Fix wave candidates: [list if any]
```

#### `findings.md` — per-scenario verdict table and bug entries

The findings file contains the full verdict matrix and one entry per finding classified CRITICAL / HIGH / MED / LOW. Verdict matrix format:

```markdown
# Batch N — Findings

> Brief one-line summary of the batch.

---

## 0. Run summary

| Item | Value |
|------|-------|
| Branch HEAD | `<commit_hash> <message>` |
| Tests | N passed / N skipped |
| Total runs | N |
| Wall-clock | ~N min |
| LLM cost (est) | ~$N |
| Driver | `<script or reyn dogfood run command>` |

---

## 1. Verdict matrix

| Scenario | V/I/R/B | Verified % | Status |
|----------|---------|-----------|--------|
| **S1** (description) | N/N/N/N | N% | ✅ / ⚠️ / ❌ |

**Gate**: ≥80% verified at N=5 — achieved / not achieved.

---

## 2. Findings

### B<N>-<S-ID>-<seq> (<severity>) — <short description>

**Severity**: CRITICAL / HIGH / MED / LOW

**Observation**: [what was observed]

**Impact**: [user-visible effect]

**Carry-over**: [fix plan or deferred reasoning]
```

Severity definitions (same as dogfood-discipline.md §2 A4):

| Severity | Meaning |
|----------|---------|
| CRITICAL | System non-functional |
| HIGH | Core user path blocked |
| MED | Degraded behavior, workaround exists |
| LOW | Cosmetic or edge-case |

#### `retrospective.md` — lessons and principle takeaways

The retrospective is the batch's durable output. It has a fixed structure:

```markdown
# Batch N — Retrospective

> One-line milestone summary.

---

## 1. Expected vs actual

| Scenario | Baseline | Prediction | Actual | Hit/Miss |
|----------|----------|-----------|--------|----------|
| S1 | V=N/N | V≥N/N | V=N/N | ✅ hit / ❌ miss |

**Batch Brier**: N.NNN

---

## 2. Turning points

### TP1: <name>

[What happened, why it mattered]

**Lesson**: [principle takeaway]

---

## 3. Principles reinforced or newly established

[Numbered list of principles confirmed or created in this batch]

---

## 4. Handoff to next batch

### Fix-wave candidates

[List of HIGH/CRITICAL findings entering fix wave]

### Optional carry-over (LOW/MED, deferred)

[List with priority reasoning]

---

## 5. Cost summary

| Item | Wall-clock | LLM cost (est) |
|------|-----------|---------------|
| ... | ... | ... |
| **Total** | | |
```

#### `report.json` — machine-readable run record

Emitted by `reyn dogfood run` (see [CLI reference](../../reference/cli/dogfood.md)). The file records the run in a structured form suitable for automated comparison and historical tracking.

Schema: see [Section 5](#5-reading-a-reportjson) below.

---

## 3. GitHub Discussions headline

### Category setup (operator, one-time)

Before the first batch Discussion is posted, a team operator must create the category in GitHub UI:

1. Go to **Discussions → New category**
2. Name: `Dogfood batches`
3. Description: `Batch-by-batch dogfood result threads`
4. Format: **Open-ended discussion**

Until the category is created, use **General** as fallback. Update the link once the category exists.

### Title format

```
Batch N (YYYY-MM-DD): <topic> — <verified_rate>% verified, <regressed_count> regressed
```

Examples:

```
Batch 27 (2026-05-17): chat router smoke + stdlib core — 75% verified, 1 regressed
Batch 26 (2026-05-16): FP-0034 wrapper-only N=5 stability — 91% verified, 0 regressed
```

### Body template

```markdown
**Batch N — YYYY-MM-DD — <topic>**

- Framework: FP-XXXX framework `<commit_hash>`
- Scenario sets: <set_name> (N) + <set_name> (N)
- Verified: N/N = N%
- Inconclusive: N
- Regressed (vs baseline `b<N>`): N (= `<scenario_id>` if any)
- Brier vs prediction: N.NN
- Journal: <link to commit of summary.md>
- Fix-wave PRs: <links if any, else "none yet">

[discussion follows in comments]
```

#### Example (filled in)

```markdown
**Batch 27 — 2026-05-17 — chat router smoke + stdlib core**

- Framework: FP-0036 framework `a1b2c3d`
- Scenario sets: chat_router_smoke (7) + stdlib_skills_core (9)
- Verified: 12/16 = 75%
- Inconclusive: 3
- Regressed (vs baseline `b26`): 1 (= `simple_capability_question`)
- Brier vs prediction: 0.21
- Journal: https://github.com/tya5/reyn/commit/<sha>
- Fix-wave PRs: none yet

[discussion follows in comments]
```

### Trailing comment: issue index

After filing all `dogfood-finding` Issues for this batch, add a trailing comment to the Discussion thread listing them:

```markdown
**Issues spawned from this batch:**

- #42 [dogfood B27] simple_capability_question: refuted — reply skips capability list [HIGH]
- #43 [dogfood B27] stdlib_core_S3: inconclusive — schema mismatch under noop [MED]
```

This makes the Discussion thread the single navigation hub for the batch.

---

## 4. GitHub Issues for actionable findings

### Label

All dogfood-sourced bugs use the label `dogfood-finding`. Create this label in GitHub UI (Labels page) before filing the first issue.

### Severity in title

Include severity in brackets so it is visible in issue lists without opening the issue:

| Severity | In title |
|----------|----------|
| CRITICAL | `[CRITICAL]` |
| HIGH | `[HIGH]` |
| MED | `[MED]` |
| LOW | `[LOW]` (omit for INFO entries — do not file Issues for INFO) |

### Title format

```
[dogfood B<N>] <scenario_id>: <symptom> [<SEVERITY>]
```

Examples:

```
[dogfood B27] simple_capability_question: reply skips capability list [HIGH]
[dogfood B27] stdlib_core_S3: schema mismatch under noop backend [MED]
[dogfood B26] S3-noop: invoke_action bypasses D14 visibility gate [LOW]
```

### Body template

```markdown
## Source

- Batch: B<N> — <YYYY-MM-DD>
- Scenario: `<scenario_id>`
- Discussion: <link to Discussion thread>
- Run: `<run_id or commit>`

## Observed vs expected

**Observed**: [concrete description of what happened]

**Expected**: [what should have happened, reference to spec/docs if applicable]

## Event log excerpt

```jsonl
{"type": "<event_type>", "data": {...}, "ts": "..."}
```

## Severity

**<SEVERITY>**: [one-sentence rationale]

## Fix hypothesis

[Initial hypothesis for root cause — label as hypothesis, not confirmed]

## Acceptance criteria

- [ ] Scenario `<scenario_id>` returns `verified` at N=3
- [ ] No regression in related scenarios
```

### Cross-link discipline

- Every `dogfood-finding` Issue links back to its Discussion thread in the body (see `## Source`).
- The Discussion thread aggregates all spawned issues in a trailing comment (see Section 3 above).

---

## 5. Reading a `report.json`

`report.json` is written by `reyn dogfood run` into the batch journal directory alongside the human-readable files. It captures the same run data in a structured form.

### Schema

```json
{
  "run_id": "<uuid>",
  "scenario_set_name": "<name>",
  "started_at": "<ISO 8601>",
  "completed_at": "<ISO 8601>",
  "framework_version": "<commit_hash or semver>",
  "n": "<repetitions per scenario>",
  "scenarios": [
    {
      "id": "<scenario_id>",
      "outcome": "verified | inconclusive | refuted | blocked",
      "repetitions": [
        {
          "run": "<repetition index>",
          "outcome": "verified | inconclusive | refuted | blocked",
          "reply_verdict": "pass | fail | inconclusive",
          "events_verdict": "pass | fail | inconclusive",
          "artifacts_verdict": "pass | fail | inconclusive"
        }
      ],
      "outcome_prediction": {
        "verified": 0.0,
        "inconclusive": 0.0,
        "refuted": 0.0,
        "blocked": 0.0
      },
      "brier": "<float>"
    }
  ],
  "aggregate": {
    "verified": "<count>",
    "inconclusive": "<count>",
    "refuted": "<count>",
    "blocked": "<count>",
    "total": "<count>",
    "verified_pct": "<float 0–100>"
  },
  "brier": "<float — batch mean Brier score>"
}
```

### Field reference

| Field | Type | Meaning |
|-------|------|---------|
| `run_id` | UUID string | Globally unique run identifier; matches `.reyn/dogfood/runs/<run_id>/` |
| `scenario_set_name` | string | Name of the scenario set YAML (e.g. `chat_router_smoke`) |
| `started_at` / `completed_at` | ISO 8601 | Run wall-clock boundaries |
| `framework_version` | string | Commit hash or semver tag at run time |
| `n` | int | Repetitions per scenario (same as `--n` flag) |
| `scenarios[].outcome` | enum | Worst-case outcome across all repetitions for this scenario |
| `scenarios[].brier` | float | Per-scenario Brier score (requires `outcome_prediction`) |
| `aggregate.verified_pct` | float | `verified / total * 100` |
| `brier` | float | Mean Brier across all scenarios with predictions |

The `brier` field is `null` if no scenario has `outcome_prediction` declared.

### Using report.json in tooling

```bash
# Extract batch Brier score
jq '.brier' report.json

# List refuted scenarios
jq '[.scenarios[] | select(.outcome == "refuted") | .id]' report.json

# Verified percentage
jq '.aggregate.verified_pct' report.json
```

---

## 6. Cross-references

- [`dogfood-discipline.md`](dogfood-discipline.md) — methodology layer: 9-principle framework, A1–A5 iterative loop, scenario design
- [`dogfood-regression-playbook.md`](dogfood-regression-playbook.md) — step-by-step run procedure, regression triage, fix-wave dispatch (R2 owns)
- [`reference/cli/dogfood.md`](../../reference/cli/dogfood.md) — CLI reference: `reyn dogfood run`, `compare`, `baseline`
- [`concepts/observability/dogfood-scenarios.md`](../../concepts/observability/dogfood-scenarios.md) — scenario set YAML schema authority
