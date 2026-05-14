# FP-0009: Operational Intelligence — Indexing Event Logs with RAG

**Status**: **Components A + C + D landed** 2026-05-15; B proposed (= FP-0001 waiting)
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Landing notes (2026-05-15)

Component A — `index_events` stdlib skill landed. P6 events are chunked per run and written to the RAG index. `recall(sources=["events"], ...)` queries are now usable from any phase. Incremental indexing via `.reyn/index/events_cursor`.

Component C — recall usage patterns documented. A dedicated section in `docs/concepts/operational-intelligence.md` covers `recall(sources=["events"])` usage.

Component D — `ops_report` stdlib skill landed. Uses the events index when available; falls back to raw event log walk when `index_events` has not been run.

Component B (periodic cron via FP-0001) remains proposed — waiting on FP-0001 A2A task lifecycle landing.

---

## Summary

By indexing P6 event logs (`.reyn/events/*.jsonl`) through the RAG infrastructure of the
`index_docs` + `recall` ops, Reyn can leverage its own execution history as a knowledge base.
Event logs that were once "records for auditing" become "operational intelligence."

The `collect_traces` / report generation / past-case lookup used by FP-0006 (skill self-improvement),
FP-0007 (evaluation infrastructure), and FP-0008 (SWE-bench) all sit naturally on this foundation.

---

## Motivation

### The Structure Created by P6 + RAG

```
Ordinary RAG:            External documents → index → recall → answer generation
                                   ↓
Operational Intelligence:  Own execution history → index → recall → self-improvement & analysis
```

P6 is append-only and holds the full execution history. With RAG Phase 1 (ADR-0033) landed,
the conditions are in place to make this history semantically searchable.

### Difference from Linear Scanning

The current approach of `read_file(events/*.jsonl)` requires reading all events.

```
With 10,000 accumulated events:
  read_file: full scan → context overflow and rising cost
  recall op: "phase2 failure patterns in my_skill" → semantically retrieves 20 relevant entries
```

The longer Reyn has been running, the less practical linear scanning becomes, and the greater
the advantage of semantic search.

### Use Cases

| Use case | Example query | Consumer |
|---|---|---|
| Skill self-improvement | "Failure patterns in the verify phase of my_skill" | FP-0006 collect_traces |
| Evaluation reporting | "Top-cost skills last week and reasons for failures" | FP-0007 |
| Past-case lookup | "Approaches that worked well in past fixes to the django repository" | FP-0008 SWE-bench |
| Debugging | "When did the last PermissionError occur and how was it resolved" | General purpose |
| Cost analysis | "Skill execution history on the day monthly cost spiked" | Operations |

---

## Core Design: Chunk Unit is "1 run"

Events are stored as JSONL with one event per line, but the meaningful unit is **1 run**
(start → complete).

```jsonl
{"type": "run_skill_started",   "data": {"skill": "my_skill", "skill_version_hash": "abc"}}
{"type": "skill_node_started",  "data": {"node": "explore"}}
{"type": "tool_executed",       "data": {"op": "grep", "status": "ok"}}
{"type": "skill_node_completed","data": {"node": "explore"}}
...
{"type": "run_skill_completed", "data": {"skill": "my_skill", "status": "success"}}
```

This is converted into a single chunk:

```
[run chunk]
skill: my_skill
version_hash: abc123
timestamp: 2026-05-10T09:15:00
status: success
duration_seconds: 43
phases: explore → plan → apply → verify → report
errors: []
tool_calls: grep(×3), read_file(×5), edit_file(×2), shell(×1)
cost_usd: 0.18
```

With this format, "failed runs," "runs where an error occurred in a specific phase," and
"high-cost runs" can all be retrieved efficiently by semantic search.

---

## Proposed implementation

### Component A — `index_events` stdlib Skill (MEDIUM)

A skill that chunks event JSONL by run and writes to the RAG index.

```
src/reyn/stdlib/skills/index_events/
  skill.md
  phases/
    scan.md          ← Identify the range of new events (incremental)
    chunk.md         ← Chunk by run unit
    index.md         ← Index using embed + index_write ops
```

**Incremental indexing mechanism**:

The last-indexed timestamp is saved to a cursor file at `.reyn/index/events_cursor`.
On the next run, only events after that timestamp are processed.

```
scan phase:
  read_file(.reyn/index/events_cursor) → last_indexed_at
  glob_files(events/*.jsonl) → list of target files
  identify new events (after last_indexed_at)

chunk phase:
  convert each run (run_skill_started → run_skill_completed) into 1 chunk
  failed runs retain error details as additional fields

index phase:
  embed op → vectorize run chunks
  index_write op → write to SqliteIndexBackend
  write_file(.reyn/index/events_cursor) → update cursor
```

**skill.md frontmatter skeleton**:

```yaml
---
name: index_events
description: Index P6 event logs by run unit — the foundation for operational intelligence
entry_phase: scan
graph:
  scan:  [chunk]
  chunk: [index]
  index: []
final_output_schema: index_events_summary
input_schema:
  since: string | null    # ISO timestamp. null = auto-retrieved from cursor
  skills: list[str] | null  # Target specific skills only. null = all skills
permissions:
  file:
    read: [".reyn/events/", ".reyn/index/"]
    write: [".reyn/index/"]   # Within the default zone, but made explicit here
---
```

### Component B — Periodic Index Updates (SMALL)

Connect to the cron mechanism from FP-0001 (A2A task lifecycle) and add a configuration
for running `index_events` on a schedule.

```yaml
# reyn.yaml
operational_intelligence:
  index_events:
    enabled: true
    schedule: "0 */6 * * *"   # Every 6 hours (default)
    skills: null               # null = all skills
```

Manual execution:
```
reyn run index_events
reyn run index_events --input '{"since": "2026-05-01T00:00:00"}'
```

### Component C — Usage Patterns from the recall Op (SMALL)

Events indexed by `index_events` can be searched directly with the existing `recall` op
(no new implementation needed).

```yaml
# From any phase in a skill
- op: recall
  query: "failure patterns in the verify phase of {{ skill_name }}"
  sources: ["events"]   # Source name registered by index_events
  top_k: 10
```

Implementation of the FP-0006 `collect_traces` phase:

```markdown
# collect_traces (implementation of FP-0006 Component C)

Retrieve failure patterns for the target skill using the recall op:
  query: "{{ input.skill_name }} failure error phase"
  sources: ["events"]
  top_k: 20

Save results as traces_summary.md in the workspace.
Falls back to read_file(events/*.jsonl) if index_events has not been run.
```

### Component D — Built-in Query Patterns (SMALL)

Provide commonly used queries as skills that users can run immediately with `reyn run`.

```
src/reyn/stdlib/skills/ops_report/
  skill.md    ← A report skill that outputs a weekly execution summary
```

Example report skill output:

```
[Weekly ops report 2026-W19]
Skills run: 5 types, 127 total executions
Success rate: 91.3% (116/127)
Average cost: $0.21 / run
Highest-failure skill: swe_bench (3/10 failures)
  → Primary cause: test execution timeout in verify phase (shell op 60s limit)
  → Recommendation: Extend safety.timeout.phase_seconds per FP-0004
```

---

## Relationship with RAG Phase 1

`index_events` is designed as an "event-log-specialized variant" of `index_docs`.

| | `index_docs` | `index_events` |
|---|---|---|
| Input source | Document files (.md / .txt / etc.) | P6 event JSONL |
| Chunk unit | LLM decides strategy (depends on document structure) | Per run (fixed) |
| Chunk content | A passage from a document | Run summary (structured) |
| Incremental | Determined by file hash changes | Timestamp cursor |
| Backend | SqliteIndexBackend (shared) | SqliteIndexBackend (shared) |

No changes to the OS layer. Implemented as a skill, so P7 compliant.

---

## Dependencies

- ADR-0033 RAG Phase 1 (landed, commit 1e6f153) — `embed` / `index_write` / `recall` ops are prerequisites
- `src/reyn/stdlib/skills/index_docs/` — reference for implementation patterns (chunkers.py approach)
- FP-0001 (A2A task lifecycle) — cron periodic execution for Component B
- FP-0006 (skill self-improvement) — `collect_traces` uses this foundation
- FP-0007 (evaluation infrastructure) — evaluation reports use this foundation
- FP-0008 (SWE-bench) — past-case lookup uses this foundation

No prerequisite PRs: ADR-0033 Phase 1 (✅ complete). FP-0001 is only a dependency for
Component B; Components A / C / D can be implemented independently.

---

## Cost estimate

**Total: MEDIUM**

| Task | Cost | Notes |
|---|---|---|
| Component A: `index_events` skill (3 phases) | MEDIUM | Main work is the run-unit chunking conversion logic |
| Component B: periodic execution config (reyn.yaml + cron) | SMALL | Requires FP-0001 |
| Component C: documenting recall op usage patterns | SMALL | No implementation needed; only skill design guide additions |
| Component D: `ops_report` skill | SMALL | Report output skill |

Bottleneck is **Component A's chunk phase** (run boundary detection and appropriate
summary formatting of failure information).

---

## Related

- `src/reyn/events/events.py` — P6 event foundation
- `src/reyn/index/` — IndexBackend + SourceManifest (ADR-0033 landed)
- `src/reyn/op_runtime/recall.py` — recall macro op (ADR-0033 landed)
- `src/reyn/stdlib/skills/index_docs/` — implementation reference
- ADR-0033 (`docs/deep-dives/decisions/0033-rag-extensible-os.md`) — RAG design
- FP-0006 (`0006-skill-self-improvement.md`) — consumer of collect_traces
- FP-0007 (`0007-evaluation-infrastructure.md`) — consumer of evaluation reports
- FP-0008 (`0008-swe-bench-integration.md`) — consumer of past-case lookup
