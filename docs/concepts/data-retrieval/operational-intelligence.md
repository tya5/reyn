---
type: concept
topic: operational-intelligence
audience: [human, agent]
---

# Operational Intelligence

Reyn's P6 audit log records every state change — phase transitions, tool calls, LLM invocations, errors — as an append-only JSONL stream. Combine that with the RAG infrastructure from ADR-0033 and the result is **operational intelligence**: Reyn agents can recall their own execution history semantically rather than via linear event log scan. The same `recall` op used for document retrieval works on execution traces once `index_events` has indexed them.

## Architecture

```
P6 events ──┐
            ├─► index_events (stdlib) ──► .reyn/index/events/ (sqlite)
            │                                      │
            │                                      ▼
            │                            recall(sources=["events"])
            │                                      │
            │           ┌──────────────────────────┼─────────────────┐
            │           ▼                          ▼                 ▼
            │      ops_report (skill)    FP-0006 collect_traces   debugging
            │      "weekly summary"      "find failure patterns"  via /chat
            │
            └─► (raw fallback for ops_report when index absent)
```

`index_events` is a stdlib skill — it requires no OS changes (P7 compliant). It reads `.reyn/events/*.jsonl`, groups events into per-run chunks, and writes them to the shared `SqliteIndexBackend`. Once indexed, any phase in any skill can query the execution history with `recall(sources=["events"], query="...", top_k=N)`.

## Run-chunk format

Events are stored one-per-line in JSONL, but the meaningful unit for operational intelligence is **one run** (from `run_skill_started` to `run_skill_completed`). `index_events` converts each run into a single structured chunk:

```
[run chunk]
skill: my_skill
version_hash: abc123...  ← sha256 of skill.md at execution time (FP-0006 A)
timestamp: 2026-05-10T09:15:00
status: success
duration_seconds: 43
phases: explore → plan → apply → verify → report
errors: []
tool_calls: grep(×3), read_file(×5), edit_file(×2), shell(×1)
cost_usd: 0.18
```

Failed runs retain error details as additional fields so queries like "failure patterns in the verify phase of my_skill" retrieve the right chunks.

## Incremental indexing

`index_events` saves the last-indexed timestamp to `.reyn/index/events_cursor`. Subsequent runs only process events that occurred after that timestamp, making repeated indexing cheap regardless of log size.

```bash
# First run — indexes everything
reyn run index_events

# Subsequent runs — only new events since last cursor
reyn run index_events

# Force a specific start date
reyn run index_events --input '{"since": "2026-05-01T00:00:00"}'
```

## Querying execution history

After `index_events` has run, the `events` source is available in `recall`:

```yaml
# From any phase in any skill
- op: recall
  query: "failure patterns in the verify phase of my_skill"
  sources: ["events"]
  top_k: 10
```

From `/chat`:

```
> What went wrong in my_skill last week?
> Which skills cost the most this month?
> Find all runs where swe_bench failed in the verify phase
```

## `skill_version_hash` and regression detection

Every `run_skill_started` event carries `skill_version_hash` — a full sha256 hex of the `skill.md` file at execution time (landed as FP-0006 Component A). This field threads through `index_events` chunks and into `reyn eval compare`.

`reyn eval compare my_skill` groups the P6 log by `skill_version_hash` and computes pass rates per version — no additional executions needed:

```
Baseline:  sha:abc12345  72% pass (36/50 runs)  2026-05-01 ~ 2026-05-05
Candidate: sha:def67890  88% pass (44/50 runs)  2026-05-05 ~ 2026-05-15
Delta:     +16pp  /  regression: none
```

See [Reference: `reyn eval compare`](../../reference/cli/eval.md#reyn-eval-compare) for the full CLI reference.

## `ops_report` — ready-made operational summary

The `ops_report` stdlib skill produces a weekly summary without requiring custom queries:

```bash
reyn run ops_report
reyn run ops_report --input '{"period_days": 30}'
```

Sample output:

```
[Weekly ops report 2026-W19]
Skills run: 5 types, 127 total executions
Success rate: 91.3% (116/127)
Average cost: $0.21 / run
Highest-failure skill: swe_bench (3/10 failures)
  → Primary cause: test execution timeout in verify phase
```

When `index_events` has not been run, `ops_report` falls back to a direct read of `.reyn/events/*.jsonl`. The indexed path is significantly faster for large logs.

## Relationship to RAG Phase 1

`index_events` is a run-log-specialised variant of `index_docs`. Both write to the same `SqliteIndexBackend`; the difference is chunk unit and incremental mechanism:

| | `index_docs` | `index_events` |
|---|---|---|
| Input | Document files (`.md`, `.txt`, …) | P6 event JSONL |
| Chunk unit | Passage (LLM decides strategy) | One run (fixed) |
| Incremental | File hash changes | Timestamp cursor (`.reyn/index/events_cursor`) |
| Backend | `SqliteIndexBackend` (shared) | `SqliteIndexBackend` (shared) |

## Scheduling (FP-0009 Component B)

`index_events` and `ops_report` benefit from running on a schedule rather than on-demand — recent operational history stays queryable without manual invocations.

### Configuration

Schedule recurring runs via reyn.yaml:

```yaml
cron:
  jobs:
    - name: index_events_hourly
      skill: index_events
      schedule: "0 */6 * * *"   # every 6 hours
      input: {}
      enabled: true

    - name: weekly_ops_report
      skill: ops_report
      schedule: "0 9 * * MON"   # Monday 09:00
      input:
        since_days: 7
```

### Running

Two modes:

- **Embedded in `reyn web`** — the scheduler starts as part of the FastAPI lifespan. Stop it by stopping the web server.
- **Foreground** — `reyn cron run` reads reyn.yaml and runs the scheduler as a long-lived foreground process. Suitable for systems that don't run the Reyn web gateway.

### Inspection

- `reyn cron list` — show configured jobs and their next-run timestamps
- `reyn cron status` — show last-run info (= only meaningful while the scheduler is up; v1 has no persistence)

### Threat model

Scheduled skills run with the same permissions as `reyn run <skill>` would (= no elevated privilege). The cron entry's `skill` and `input` are operator-controlled via reyn.yaml; per-skill credential scoping (FP-0016 D) still applies. The scheduler does NOT bypass the permission system.

### Cross-references

- `docs/reference/cli/cron.md` — `reyn cron` CLI reference
- `docs/reference/config/reyn-yaml.md` — `cron:` block schema
- `docs/concepts/multi-agent/a2a.md` — `RunRegistry` pattern (= sibling lifecycle abstraction)

## See also

- [FP-0009: Operational Intelligence](../../deep-dives/proposals/0009-operational-intelligence.md) — full design rationale
- [FP-0006: Skill Self-Improvement](../../deep-dives/proposals/0006-skill-self-improvement.md) — `skill_version_hash` contract
- [FP-0007: Evaluation Infrastructure](../../deep-dives/proposals/0007-evaluation-infrastructure.md) — `reyn eval compare` design
- [Concepts: RAG](../data-retrieval/rag.md) — underlying index/recall primitives
- [Concepts: Events](../runtime/events.md) — P6 event log structure
- [Reference: `reyn eval compare`](../../reference/cli/eval.md#reyn-eval-compare) — CLI reference
