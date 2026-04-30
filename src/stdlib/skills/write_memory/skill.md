---
type: skill
name: write_memory
description: |
  Extract durable memories from a conversation segment and persist them to
  the appropriate scope (global or per-project) as markdown files plus
  MEMORY.md index entries.
entry: extract
final_output: memory_extract_result
final_output_description: |
  Summary of what was created, updated, deleted, or skipped. The file
  mutations themselves are performed via Control IR file ops; this result is
  a record of decisions for the caller.
finish_criteria:
  - Each scope_dir's MEMORY.md has been read (or confirmed absent)
  - The conversation segment was analyzed for memorable facts
  - Each new/updated memory has been written to disk and indexed
  - actions list reflects all decisions, including "none" when nothing was extracted
graph:
  extract: []
---

## Overview

LLM-based memory extraction. Reads existing memory indexes for context,
analyzes a conversation segment, and writes new/updated memories using
typed categories (user / feedback / project / reference).

## Input

`memory_extract_request` artifact:

- `conversation_segment` — list of `{role, text, ts}` to mine
- `scope_dirs` — `[{path, scope}]` writable destinations

## Output

`memory_extract_result.actions` — record of each decision.

## Storage format produced

Per-memory file `<slug>.md`:

```markdown
---
name: <Title>
description: <one-line summary>
type: user|feedback|project|reference
---

<full body>
```

`MEMORY.md` index (one line per memory):

```
- [Name](slug.md) — description
```
