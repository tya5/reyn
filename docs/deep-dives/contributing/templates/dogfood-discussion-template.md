# GitHub Discussion — Dogfood Batch Headline Template

<!-- This file is an operator reference, not a published doc.
     Copy the body block below when creating a new GitHub Discussion thread.

     Setup (one-time):
       1. GitHub UI → Discussions → New category
       2. Name: "Dogfood batches"
       3. Description: "Batch-by-batch dogfood result threads"
       4. Format: Open-ended discussion
     Until the category exists, use "General" as fallback.

     Label (one-time):
       Create a label "dogfood-finding" in GitHub UI (Labels page)
       before filing the first issue. -->

---

## Discussion title

```
Batch <N> (<YYYY-MM-DD>): <topic> — <verified_pct>% verified, <regressed_count> regressed
```

Example:

```
Batch 27 (2026-05-17): chat router smoke + stdlib core — 75% verified, 1 regressed
```

---

## Discussion body (paste this into GitHub)

```markdown
**Batch <N> — <YYYY-MM-DD> — <topic>**

- Framework: <FP-XXXX> framework `<commit_hash>`
- Scenario sets: <set_name_1> (<count>) [+ <set_name_2> (<count>)]
- Verified: <count>/<total> = <pct>%
- Inconclusive: <count>
- Regressed (vs baseline `b<prev_N>`): <count> [= `<scenario_id>` if count > 0]
- Brier vs prediction: <float>
- Journal: <URL to commit containing summary.md>
- Fix-wave PRs: <PR links, or "none yet">

[discussion follows in comments]
```

---

## Issue template (file one per HIGH/CRITICAL finding)

**Title format**:

```
[dogfood B<N>] <scenario_id>: <symptom> [<SEVERITY>]
```

**Labels**: `dogfood-finding`

**Body**:

```markdown
## Source

- Batch: B<N> — <YYYY-MM-DD>
- Scenario: `<scenario_id>`
- Discussion: <URL to Discussion thread>
- Run: `<run_id or commit_hash>`

## Observed vs expected

**Observed**: <concrete description of what happened>

**Expected**: <what should have happened>

## Event log excerpt

\`\`\`jsonl
{"type": "<event_type>", "data": {...}, "ts": "..."}
\`\`\`

## Severity

**<SEVERITY>**: <one-sentence rationale>

## Fix hypothesis

<Initial hypothesis — label as hypothesis, not confirmed root cause>

## Acceptance criteria

- [ ] Scenario `<scenario_id>` returns `verified` at N=3
- [ ] No regression in related scenarios
```

---

## Trailing comment (add after all issues are filed)

Post this as a comment on the Discussion thread once all issues are filed for this batch:

```markdown
**Issues spawned from this batch:**

- #<N> [dogfood B<N>] <scenario_id>: <symptom> [<SEVERITY>]
- #<N> [dogfood B<N>] <scenario_id>: <symptom> [<SEVERITY>]

<!-- Add one line per issue. This comment makes the Discussion the navigation hub for the batch. -->
```
