---
type: skill
name: recall_memory
description: |
  Find memories relevant to a query. Reads MEMORY.md indexes from each
  scope_dir, asks the LLM to score entries by relevance, then loads and
  returns the top matches.
entry: pick
final_output: memory_recall_result
final_output_description: |
  The memories deemed most relevant to the query, with their full content
  inlined. Empty list when no memories exist or none are relevant.
finish_criteria:
  - At least one MEMORY.md index has been examined (or all scope_dirs were missing)
  - Each returned memory has its content loaded from disk
  - Irrelevant entries are filtered out
graph:
  pick: []
---

## Overview

LLM-based memory retrieval. No vector search. The picker looks at each
indexed memory's `name` and one-line description, scores relevance against
the query, and returns the top-K with full content.

## Input

`memory_query` artifact:

- `query` — the string to find memories for
- `recent_history` — optional context turns
- `scope_dirs` — list of absolute paths to memory directories
- `top_k` — optional cap on returned memories (default 5)

## Output

`memory_recall_result.relevant` — list of `{name, type, source, content, score}`.

## Storage format expected

Each `scope_dir` should contain:

- `MEMORY.md` — index file (one line per memory: `- [Name](file.md) — desc`)
- `<slug>.md` — per-memory file with frontmatter `{name, description, type}`

Missing dirs and missing `MEMORY.md` are silently ignored — recall is
best-effort.
