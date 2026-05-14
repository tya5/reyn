---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [index_events]
---

# `index_events`

Index the P6 event log at run granularity into the RAG infrastructure for operational intelligence queries (FP-0009 Component A).

## Entry

`scan`

## Final output

`index_events_summary` — `indexed_runs`, `skipped_runs`, `filtered_runs`, and `new_cursor` (ISO timestamp written to `.reyn/index/events_cursor`).

## How it composes

Two-stage execution. Phase `scan` (LLM): a preprocessor runs `resolve_scan_context` to read the cursor file and summarise the event file inventory (count + timestamp range, no paths exposed); the LLM echoes the resolved `since`, `event_files_count`, `skill_filter`, and `mode` into `scan_plan` and finishes immediately. Skill.postprocessor (deterministic, no LLM): `run_collect_chunks` walks `.reyn/events/` from the cursor, groups events by run boundary (1 run = 1 chunk), writes `event_chunks.jsonl`; the `embed` op embeds each chunk; `index_write` writes to the `events` source; `run_advance_cursor` persists the new cursor.

## Caveats

- Requires `python` (unsafe) permissions for `resolve_scan_context`, `run_collect_chunks`, and `run_advance_cursor`.
- Incremental by default: only runs completed after the last cursor value are processed. Pass `mode: replace` to full-reindex.
- Incomplete runs (no completion event yet) are skipped and deferred to the next invocation.
- The source name `events` is hardcoded in the postprocessor's `index_write` step — this is an internal convention, not a P7 violation (the source name is not in OS code).

## Usage

Use to make Reyn execution history searchable via `recall` queries. Run without arguments for incremental indexing; supply `since` or `skills` to narrow scope.

```bash
reyn run index_events
reyn run index_events --input '{"since":"2026-05-14T00:00:00"}'
reyn run index_events --input '{"skills":["my_skill"],"mode":"replace"}'
```

To recall indexed events from a skill or phase:

```yaml
- op: recall
  query: "my_skill failure error phase"
  sources: ["events"]
  top_k: 10
```

## Source

[`src/stdlib/skills/index_events/skill.md`](https://github.com/tya5/reyn/blob/main/src/stdlib/skills/index_events/skill.md)
