---
type: concept
topic: operational-intelligence
audience: [human, agent]
---

# Operational Intelligence

Reyn's P6 audit log records every state change — phase transitions, tool calls, LLM invocations, errors — as an append-only JSONL stream. Combine that with the RAG infrastructure from ADR-0033 and the result is **operational intelligence**: Reyn agents can recall their own execution history semantically rather than via linear event log scan. The same `recall` op used for document retrieval works on execution traces once the event log has been indexed into a source — using the same [`embed_and_index()`](rag.md) primitive as any other corpus, there is no dedicated events-indexing skill.

## Architecture

```
P6 events ──┐
            ├─► your indexing step ──► embed_and_index(source="events") ──► .reyn/cache/index/events/ (sqlite)
            │                                                                        │
            │                                                                        ▼
            │                                                          recall(sources=["events"])
            │                                                                        │
            │                                       ┌────────────────────────────────┼─────────────────┐
            │                                       ▼                                ▼                 ▼
            │                            your own analysis phase      FP-0006 collect_traces      debugging
            │                            (no bundled "weekly summary") "find failure patterns"     via /chat
            └─► raw file read fallback (`.reyn/events/*.jsonl`) when no index exists
```

Indexing the event log is not a bundled skill — write a `python` step that reads `.reyn/events/*.jsonl`, groups events into per-run chunks, and calls `embed_and_index(chunks, source="events", ...)` the same way you would for any other corpus (see [Concepts: RAG — Quick start](rag.md#quick-start)). Once indexed, any phase can query the execution history with `recall(sources=["events"], query="...", top_k=N)`.

## Run-chunk format

Events are stored one-per-line in JSONL, but the meaningful unit for operational intelligence is **one run** (from `run_skill_started` to `run_skill_completed`). Group each run into a single structured chunk before calling `embed_and_index`:

```
[run chunk]
agent: my_agent
timestamp: 2026-05-10T09:15:00
status: success | failed | aborted
tool_calls: grep(×3), read_file(×5), edit_file(×2), shell(×1)
cost_usd: 0.18   ← aggregated from llm_response_received.cost_usd across the run
```

The exact fields available depend on which P6 event types you fold into a chunk — see [Concepts: Events](../runtime/events.md) for the current event taxonomy (`session_started`/`session_completed`, `turn_started`/`turn_completed`, `llm_response_received`, tool-call events). There is no single ready-made "one event per run" summary event; building a run-chunk means aggregating the session/turn boundary events yourself in your indexing step.

Failed runs should retain error details as additional chunk metadata so queries like "failure patterns in my_agent" retrieve the right chunks.

## Incremental indexing

Use `embed_and_index`'s `mode="append"` (see [Concepts: RAG — Limitations](rag.md#limitations)) and track your own last-indexed timestamp (e.g. a cursor file under `.reyn/cache/`) so repeated indexing only processes events since the last run.

## Querying execution history

Once a source has been indexed, `recall` can query it from any phase:

```yaml
- type: run_op
  op:
    kind: recall
    query: "failure patterns in my_agent"
    sources: ["events"]
    top_k: 10
  output_name: trace_summary
```

From `/chat`:

```
> What went wrong last week?
> Find all runs where the agent failed during file edits
```

## Relationship to RAG Phase 1

Indexing the event log uses the exact same `embed_and_index()` entry point as indexing documents (see [Concepts: RAG](rag.md)) — the only difference is what you chunk (one chunk per run, instead of per passage) and how you track incremental progress (a timestamp cursor, instead of `content_hash` dedup).

## Scheduling

Recurring indexing (and any reporting built on top of it) is not a bundled feature — `reyn.yaml`'s `cron:` jobs dispatch a message to a named **agent** (`to`/`message`, not a skill invocation), so keeping the events index current on a schedule means having an agent whose task includes running your indexing step:

```yaml
cron:
  jobs:
    - name: reindex_events_hourly
      to: ops_agent
      message: "reindex the events source, then summarize failures since last run"
      schedule: "0 */6 * * *"   # every 6 hours
      enabled: true
```

See [Reference: `reyn cron`](../../reference/cli/cron.md) and [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) for the current job schema, running modes, and inspection commands.

## See also

- [FP-0009: Operational Intelligence](../../deep-dives/proposals/0009-operational-intelligence.md) — original design rationale (predates the skill-word removal; primitives described here are current, skill-based examples are not)
- [Concepts: RAG](rag.md) — underlying index/recall primitives
- [Concepts: Events](../runtime/events.md) — P6 event log structure and current event taxonomy
